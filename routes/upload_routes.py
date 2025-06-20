import logging
import mimetypes
import os
import uuid
import time
import json
from typing import Dict, Any, Tuple, Optional, List
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import (
    Blueprint, request, make_response, jsonify, Response, stream_with_context, url_for
)
from flask_jwt_extended import jwt_required, get_jwt_identity
from werkzeug.utils import secure_filename
from bson import ObjectId

from database import (
    User, find_user_by_id, save_file_metadata,
    get_metadata_collection, find_metadata_by_access_id
)
from extensions import upload_progress_data
from config import (
    TELEGRAM_CHAT_IDS, PRIMARY_TELEGRAM_CHAT_ID,
    MAX_UPLOAD_WORKERS, TELEGRAM_MAX_CHUNK_SIZE_BYTES,
    format_bytes
)
from telegram_api import send_file_to_telegram
from google_drive_api import (
    StreamReaderWrapper,
    delete_from_gdrive,
    upload_to_gdrive_with_progress,
    download_from_gdrive_to_file,
    initiate_resumable_gdrive_session              # NEW
)
from .utils import _yield_sse_event, _safe_remove_file

# ---------------------------------------------------------------------------
# Blueprint / thread-pool setup
# ---------------------------------------------------------------------------
telegram_transfer_executor = ThreadPoolExecutor(
    max_workers=MAX_UPLOAD_WORKERS,
    thread_name_prefix='BgTgTransfer'
)
ApiResult = Tuple[bool, str, Optional[Dict[str, Any]]]
upload_bp = Blueprint('upload', __name__)

# ---------------------------------------------------------------------------
# Helpers (unchanged)
# ---------------------------------------------------------------------------
def _parse_send_results(log_prefix: str, send_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    all_chat_details: List[Dict[str, Any]] = []
    for res in send_results:
        detail: Dict[str, Any] = {"chat_id": res["chat_id"], "success": res["success"]}
        if res["success"] and res.get("tg_response"):
            r = res["tg_response"].get('result', {})
            msg_id = r.get('message_id')
            f_id = r.get('document', {}).get('file_id')
            f_uid = r.get('document', {}).get('file_unique_id')
            if msg_id and f_id and f_uid:
                detail.update({
                    "message_id": msg_id,
                    "file_id":      f_id,
                    "file_unique_id": f_uid
                })
                size = r.get('document', {}).get('file_size')
                if size is not None:
                    detail["file_size"] = size
            else:
                detail.update({"success": False, "error": "Missing IDs in Telegram response"})
        elif not res["success"]:
            detail["error"] = res["message"]
        all_chat_details.append(detail)
    return all_chat_details


def _send_single_file_task(file_path: str, filename: str, chat_id: str, upload_id: str) -> Tuple[str, ApiResult]:
    with open(file_path, 'rb') as f:
        return str(chat_id), send_file_to_telegram(f, filename, chat_id)


def _send_chunk_task(chunk_data: bytes, filename: str, chat_id: str, upload_id: str, chunk_num: int) -> Tuple[str, ApiResult]:
    import io
    with io.BytesIO(chunk_data) as buf:
        return str(chat_id), send_file_to_telegram(buf, filename, chat_id)


# ============================================================================
#  NEW 1/3  –  Initiate Google-Drive Resumable Session
# ============================================================================
@upload_bp.route('/initiate-gdrive-session', methods=['POST', 'OPTIONS'])
@jwt_required(optional=True)
def initiate_gdrive_session():
    """Front-end obtains a Drive resumable-upload session URI."""
    if request.method == 'OPTIONS':
        return make_response(("OK", 200))

    data = request.get_json() or {}
    filename  = data.get("filename")
    filesize  = data.get("filesize")
    mimetype  = data.get("mimetype")

    if not filename or filesize is None or not mimetype:
        return jsonify({"error": "Required: filename, filesize, mimetype"}), 400
    try:
        filesize_int = int(filesize)
    except ValueError:
        return jsonify({"error": "'filesize' must be integer"}), 400

    session_uri, err = initiate_resumable_gdrive_session(
        filename=filename,
        filesize=filesize_int,
        mimetype=mimetype
    )

    if err or not session_uri:
        logging.error(f"[InitGDriveSession] {err}")
        return jsonify({"error": err or "Failed to create Drive session"}), 500

    return jsonify({"session_uri": session_uri}), 200


# ============================================================================
#  NEW 2/3  –  Register a completed direct-to-Drive upload
# ============================================================================
@upload_bp.route('/register-gdrive-upload', methods=['POST'])
@jwt_required(optional=True)
def register_gdrive_upload():
    """
    Front-end calls this *after* uploading file data directly to Google Drive
    (using the session URI).  We persist metadata so the file appears in the
    batch and can later be pushed to Telegram.
    """
    data = request.get_json() or {}
    batch_id          = data.get("batch_id")
    gdrive_file_id    = data.get("gdrive_file_id")
    original_filename = data.get("original_filename")
    original_size     = data.get("original_size")

    if not all([batch_id, gdrive_file_id, original_filename, original_size]):
        return jsonify({"error": "Fields required: batch_id, gdrive_file_id, original_filename, original_size"}), 400

    # ensure size is an int
    try:
        original_size_int = int(original_size)
    except ValueError:
        return jsonify({"error": "'original_size' must be integer"}), 400

    # fetch batch
    record, err = find_metadata_by_access_id(batch_id)
    if err or record is None:
        return jsonify({"error": "Batch not found"}), 404

    # create file details in our canonical schema
    file_details = {
        "original_filename": original_filename,
        "gdrive_file_id":    gdrive_file_id,
        "original_size":     original_size_int,
        "mime_type": mimetypes.guess_type(original_filename)[0] or "application/octet-stream",
        "telegram_send_status": "pending"
    }

    coll, db_err = get_metadata_collection()
    if db_err:
        return jsonify({"error": "DB error"}), 500

    upd = coll.update_one(
        {"access_id": batch_id},
        {"$push": {"files_in_batch": file_details}}
    )
    if upd.matched_count == 0:
        return jsonify({"error": "Batch not found"}), 404

    logging.info(f"[RegisterUpload] Added file to batch {batch_id}: {original_filename}")
    return jsonify({"message": "File registered successfully"}), 200


# ============================================================================
#  PROGRESS SSE  (unchanged)
# ============================================================================
@upload_bp.route('/progress-stream/<batch_id>', methods=['GET'])
def stream_upload_progress(batch_id: str):
    """SSE for real-time upload progress."""
    def generate_events():
        last_data = None
        log_prefix = f"[ProgressStream-{batch_id}]"
        logging.info(f"{log_prefix} SSE opened.")
        last_hb = time.time()
        hb_int  = 15
        try:
            while True:
                evt = upload_progress_data.get(batch_id)
                if evt and evt != last_data:
                    yield _yield_sse_event(evt.get("type", "status"), evt)
                    last_data = evt
                    last_hb   = time.time()
                    if evt.get("type") in ("complete", "error", "finalized"):
                        break
                if time.time() - last_hb > hb_int:
                    yield ": heartbeat\n\n"
                    last_hb = time.time()
                time.sleep(0.2)
        finally:
            upload_progress_data.pop(batch_id, None)
    return Response(stream_with_context(generate_events()), mimetype='text/event-stream')


# ============================================================================
#  Batch initiation endpoint  (unchanged)
# ============================================================================
@upload_bp.route('/initiate-batch', methods=['POST', 'OPTIONS'])
@jwt_required(optional=True)
def initiate_batch_upload():
    if request.method == 'OPTIONS':
        return make_response(("OK", 200))
    # ... (body unchanged for brevity)
    batch_id = str(uuid.uuid4())
    log_prefix = f"[BatchInitiate-{batch_id}]"
    # (rest of code remains identical to earlier version)
    # -----------------------------------------------------------------------
    user_info = {"is_anonymous": True, "username": "Anonymous", "user_email": None}
    identity = get_jwt_identity()
    if identity:
        u_doc, _ = find_user_by_id(ObjectId(identity))
        if u_doc:
            user_info.update({
                "is_anonymous": False,
                "username": u_doc.get("username"),
                "user_email": u_doc.get("email")
            })

    data = request.get_json() or {}
    payload = {
        "access_id": batch_id,
        "username": user_info["username"],
        "user_email": user_info["user_email"],
        "is_anonymous": user_info["is_anonymous"],
        "upload_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "storage_location": "gdrive",
        "status_overall": "batch_initiated",
        "is_batch": data.get("is_batch", True),
        "batch_display_name": data.get("batch_display_name", "Unnamed Batch"),
        "files_in_batch": [],
        "total_original_size": data.get("total_original_size", 0),
    }

    success, msg = save_file_metadata(payload)
    if not success:
        logging.error(f"{log_prefix} DB error: {msg}")
        return jsonify({"error": f"Failed to initiate batch: {msg}"}), 500

    logging.info(f"{log_prefix} Batch created for {user_info['username']}")
    return jsonify({"message": "Batch initiated", "batch_id": batch_id}), 201


# -----------------------------------------------------------------------------
#   STREAM FILE TO BATCH  (logic fixed to capture generator return)
# -----------------------------------------------------------------------------
@upload_bp.route('/stream', methods=['POST', 'OPTIONS'])
@jwt_required(optional=True)
def stream_file_to_batch():
    """
    True streaming: wrap request.stream in StreamReaderWrapper,
    read filesize from X-Filesize header, and pipe directly to GDrive.
    """
    if request.method == 'OPTIONS':
        return make_response(("OK", 200))

    # --- 1) Basic param / header validation ---------------------------------
    batch_id = request.args.get('batch_id')
    if not batch_id:
        return jsonify({"error": "Missing 'batch_id' param"}), 400

    filename = secure_filename(request.args.get('filename', ''))
    if not filename:
        return jsonify({"error": "Missing 'filename' param"}), 400

    size_hdr = request.headers.get('X-Filesize')
    if not size_hdr:
        return jsonify({"error": "Missing 'X-Filesize' header"}), 400
    try:
        total_size = int(size_hdr)
    except ValueError:
        return jsonify({"error": "Invalid 'X-Filesize' header"}), 400

    log_prefix = f"[StreamForBatch-{batch_id}]"
    gdrive_id: Optional[str] = None  # will be filled later

    # --- 2) Main processing --------------------------------------------------
    try:
        # 2-a) Build wrapper & generator
        wrapper = StreamReaderWrapper(request.stream, total_size)
        gen = upload_to_gdrive_with_progress(
            source=wrapper,
            filename_in_gdrive=filename,
            operation_id_for_log=batch_id
        )

        err: Optional[str] = None  # initialize for later

        # 2-b) Manually drive generator so we catch StopIteration ourselves
        while True:
            try:
                evt = next(gen)              # may raise StopIteration
                evt["filename"] = filename
                upload_progress_data[batch_id] = evt
            except StopIteration as e:
                gdrive_id, err = e.value      # type: ignore[attr-defined]
                break

        # 2-c) Validate result
        if err or not gdrive_id:
            raise Exception(err or "No Drive ID returned from upload generator")

        logging.info(f"{log_prefix} Upload finished. Drive ID = {gdrive_id}")

        # --- 3) Save metadata ------------------------------------------------
        details = {
            "original_filename": filename,
            "gdrive_file_id": gdrive_id,
            "original_size": total_size,
            "mime_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
            "telegram_send_status": "pending"
        }
        coll, db_err = get_metadata_collection()
        if db_err:
            raise Exception(db_err)

        res = coll.update_one(
            {"access_id": batch_id},
            {"$push": {"files_in_batch": details}}
        )
        if res.matched_count == 0:
            delete_from_gdrive(gdrive_id)
            raise Exception("Batch ID not found while saving metadata")

    except Exception as ex:
        # -------- Error handling & cleanup -----------------------------------
        if gdrive_id:
            delete_from_gdrive(gdrive_id)
        logging.error(f"{log_prefix} Error: {ex}", exc_info=True)
        upload_progress_data[batch_id] = {"type": "error", "message": str(ex)}
        return jsonify({"error": str(ex)}), 500

    # --- 4) Success response -------------------------------------------------
    upload_progress_data[batch_id] = {"type": "status", "message": f"Completed: {filename}"}
    return jsonify({"message": f"'{filename}' uploaded to GDrive"}), 200


# -----------------------------------------------------------------------------
#   FINALIZE BATCH (unchanged)
# -----------------------------------------------------------------------------
@upload_bp.route('/finalize-batch/<batch_id>', methods=['POST'])
@jwt_required(optional=True)
def finalize_batch_upload(batch_id: str):
    log_prefix = f"[BatchFinalize-{batch_id}]"
    upload_progress_data[batch_id] = {"type": "finalized", "message": "Starting Telegram transfer"}
    coll, err = get_metadata_collection()
    if err:
        return jsonify({"error": "DB error"}), 500
    upd = coll.update_one(
        {"access_id": batch_id},
        {"$set": {"status_overall": "gdrive_complete_pending_telegram"}}
    )
    if upd.matched_count == 0:
        return jsonify({"error": "Batch not found"}), 404

    logging.info(f"{log_prefix} Submitting background Telegram transfer")
    telegram_transfer_executor.submit(run_gdrive_to_telegram_transfer, batch_id)

    base = os.environ.get('FRONTEND_URL', '').rstrip('/')
    download_url = f"{base}/batch-view/{batch_id}"
    return jsonify({
        "message": "Batch finalized",
        "access_id": batch_id,
        "download_url": download_url
    }), 202


# -----------------------------------------------------------------------------
#   RUN TELEGRAM TRANSFER (unchanged)
# -----------------------------------------------------------------------------
def run_gdrive_to_telegram_transfer(access_id: str):           
    log_prefix = f"[BG-TG-{access_id}]"
    logging.info(f"{log_prefix} Starting background transfer")
    record, err = find_metadata_by_access_id(access_id)
    if err or not record:
        logging.error(f"{log_prefix} DB fetch error")
        return

    try:
        if record.get("status_overall") != "gdrive_complete_pending_telegram":
            return
        record["status_overall"] = "telegram_processing_background"
        save_file_metadata(record)

        files = record.get("files_in_batch", [])
        updated = []
        all_success = True

        with ThreadPoolExecutor(
            max_workers=MAX_UPLOAD_WORKERS, 
            thread_name_prefix=f'TgSend_{access_id[:4]}'
        ) as executor:
            for fmeta in files:
                fname = fmeta.get("original_filename")
                gid = fmeta.get("gdrive_file_id")
                temp_path: Optional[str] = None
                new_meta = fmeta.copy()

                try:
                    # download to temp
                    import tempfile
                    suffix = os.path.splitext(fname)[1]
                    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                        temp_path = tmp.name
                    ok, dl_err = download_from_gdrive_to_file(gid, temp_path)
                    if not ok:
                        raise Exception(dl_err)

                    size = os.path.getsize(temp_path)
                    if size > TELEGRAM_MAX_CHUNK_SIZE_BYTES:
                        # chunked upload
                        chunks = []
                        part = 1
                        success_primary = True
                        with open(temp_path, 'rb') as fh:
                            while True:
                                data = fh.read(TELEGRAM_MAX_CHUNK_SIZE_BYTES)
                                if not data:
                                    break
                                chunk_name = f"{fname}.{str(part).zfill(3)}"
                                futs = {
                                    executor.submit(
                                        _send_chunk_task, data, chunk_name, str(cid), access_id, part
                                    ): cid for cid in TELEGRAM_CHAT_IDS
                                }
                                results = []
                                for fut in as_completed(futs):
                                    cid = futs[fut]
                                    _, api = fut.result()
                                    results.append({
                                        "chat_id": cid,
                                        "success": api[0],
                                        "message": api[1],
                                        "tg_response": api[2]
                                    })
                                parsed = _parse_send_results(f"{log_prefix} {chunk_name}", results)
                                if not any(r["success"] and str(r["chat_id"]) == str(PRIMARY_TELEGRAM_CHAT_ID) for r in parsed):
                                    success_primary = False
                                    break
                                chunks.append({
                                    "part_number": part,
                                    "send_locations": parsed
                                })
                                part += 1
                        if success_primary:
                            new_meta["telegram_send_status"] = "success_chunked"
                            new_meta["telegram_chunks"] = chunks
                        else:
                            raise Exception("Chunked send failed")
                    else:
                        # single upload
                        futs = {
                            executor.submit(
                                _send_single_file_task, temp_path, fname, str(cid), access_id
                            ): cid for cid in TELEGRAM_CHAT_IDS
                        }
                        results = []
                        for fut in as_completed(futs):
                            cid = futs[fut]
                            _, api = fut.result()
                            results.append({
                                "chat_id": cid,
                                "success": api[0],
                                "message": api[1],
                                "tg_response": api[2]
                            })
                        parsed = _parse_send_results(f"{log_prefix} {fname}", results)
                        if any(r["success"] and str(r["chat_id"]) == str(PRIMARY_TELEGRAM_CHAT_ID) for r in parsed):
                            new_meta["telegram_send_status"] = "success_single"
                            new_meta["telegram_send_locations"] = parsed
                        else:
                            raise Exception("Single send failed")

                    # cleanup
                    delete_from_gdrive(gid)
                except Exception as e:
                    logging.error(f"{log_prefix} Error for {fname}: {e}", exc_info=True)
                    new_meta["telegram_send_status"] = "error"
                    new_meta["reason"] = str(e)
                    all_success = False
                finally:
                    if temp_path:
                        try:
                            os.remove(temp_path)
                        except:
                            pass
                updated.append(new_meta)

        record["files_in_batch"] = updated
        record["status_overall"] = "telegram_complete" if all_success else "telegram_processing_errors"
        save_file_metadata(record)

    except Exception as bg_ex:
        logging.error(f"{log_prefix} Unhandled BG error: {bg_ex}", exc_info=True)
        record["status_overall"] = "error_bg"
        record["last_error"] = str(bg_ex)
        save_file_metadata(record)

@upload_bp.route('/stream-legacy', methods=['POST', 'OPTIONS'])
@jwt_required(optional=True)
def stream_upload_to_gdrive():
    # Legacy handler left intact
    if request.method == 'OPTIONS':
        return make_response(("OK", 200))

    operation_id = str(uuid.uuid4())
    log_prefix = f"[StreamUpload-{operation_id}]"

    def generate_events():
        gdrive_id = None
        try:
            identity = get_jwt_identity()
            user_info = {"is_anonymous": True, "username": "Anonymous", "user_email": None}
            if identity:
                u_doc, _ = find_user_by_id(ObjectId(identity))
                if u_doc:
                    user_info.update({
                        "is_anonymous": False,
                        "username": u_doc.get("username"),
                        "user_email": u_doc.get("email")
                    })

            filename = secure_filename(request.args.get('X-Filename', ''))
            size = int(request.args.get('X-Filesize', 0))
            if not filename:
                yield _yield_sse_event("error", {"message": "Filename missing"})
                return

            upload_progress_data[operation_id] = {}
            buf = io.BytesIO(request.stream.read())
            for evt in upload_to_gdrive_with_progress(
                source=buf,
                filename_in_gdrive=filename,
                operation_id_for_log=operation_id
            ):
                if evt.get("type") == "error":
                    raise Exception(evt.get("message"))
                yield _yield_sse_event(evt.get("type", "status"), evt)

            final = upload_progress_data.get(operation_id, {})
            gdrive_id = final.get("gdrive_file_id_temp_result")
            if not gdrive_id:
                raise Exception("No Drive ID returned")

            db_payload = {
                "access_id": operation_id,
                "username": user_info["username"],
                "user_email": user_info["user_email"],
                "is_anonymous": user_info["is_anonymous"],
                "upload_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "storage_location": "gdrive",
                "status_overall": "gdrive_complete_pending_telegram",
                "is_batch": False,
                "batch_display_name": filename,
                "files_in_batch": [{
                    "original_filename": filename,
                    "gdrive_file_id": gdrive_id,
                    "original_size": size,
                    "mime_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
                    "telegram_send_status": "pending"
                }],
                "total_original_size": size
            }
            ok, db_msg = save_file_metadata(db_payload)
            if not ok:
                delete_from_gdrive(gdrive_id)
                raise Exception(db_msg)

            base = os.environ.get('FRONTEND_URL', '').rstrip('/')
            download_url = f"{base}/batch-view/{operation_id}" or url_for('download_prefixed.stream_download_by_access_id', access_id=operation_id, _external=True)
            yield _yield_sse_event("complete", {
                "message": "Uploaded successfully",
                "access_id": operation_id,
                "download_url": download_url,
                "gdrive_file_id": gdrive_id
            })
            logging.info(f"{log_prefix} Completed {filename}")

        except Exception as e:
            logging.error(f"{log_prefix} Error: {e}", exc_info=True)
            if gdrive_id:
                delete_from_gdrive(gdrive_id)
            yield _yield_sse_event("error", {"message": str(e)})
        finally:
            upload_progress_data.pop(operation_id, None)

    return Response(stream_with_context(generate_events()), mimetype='text/event-stream')
