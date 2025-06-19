# google_drive_api.py – OAuth-user version (free Gmail)
# --------------------------------------------------------------
# Auth flow now uses an OAuth2 refresh-token tied to your personal
# Gmail account instead of a service account, so uploads count
# against your main Drive quota.  Service-account imports have
# been removed and _get_drive_service() now builds a Credentials
# object from CLIENT_ID / CLIENT_SECRET / REFRESH_TOKEN.

import os
import io
import json               # NEW
import logging
import mimetypes
import requests           # NEW
from typing import Optional, Tuple, Generator, Dict, Any, Union

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import (
    MediaFileUpload,
    MediaIoBaseUpload,
    MediaIoBaseDownload,
)
from google_auth_httplib2 import AuthorizedHttp
import httplib2

# ---------------------------------------------------------------------------
# Stream wrapper for non-seekable inputs
# ---------------------------------------------------------------------------
class StreamReaderWrapper:
    """
    Wrap a non-seekable stream (e.g. Flask request.stream) so that it presents
    the minimal IO interface google-api-python-client expects.  We expose
    read(), __len__(), tell(), seekable() and a *dummy* seek().  The client
    library still probes for seek() even when we declare the stream as
    non-seekable; returning 0 prevents AttributeError crashes.
    """
    def __init__(self, stream_obj, stream_len: int):
        self.stream_obj = stream_obj
        self.stream_len = stream_len
        self._bytes_read = 0

    # --- Required read interface ---------------------------------------------
    def read(self, size: int = -1) -> bytes:
        data = self.stream_obj.read(size)
        read_len = len(data) if data else 0
        self._bytes_read += read_len
        return data

    # --- Size helpers ---------------------------------------------------------
    def __len__(self) -> int:
        return self.stream_len

    def tell(self) -> int:
        """Return bytes already read (for progress calculations)."""
        return self._bytes_read

    # --- New IOBase-compat methods -------------------------------------------
    def seekable(self) -> bool:
        """Return False — this stream cannot seek."""
        return False

    def seek(self, offset: int, whence: int = 0) -> int:  # noqa: D401, E251
        """No-op seek; always return 0 so the caller doesn’t crash."""
        return 0

# ---------------------------------------------------------------------------
# Environment & constants
# ---------------------------------------------------------------------------
load_dotenv()

GDRIVE_CLIENT_ID     = os.getenv("GDRIVE_CLIENT_ID")
GDRIVE_CLIENT_SECRET = os.getenv("GDRIVE_CLIENT_SECRET")
GDRIVE_REFRESH_TOKEN = os.getenv("GDRIVE_REFRESH_TOKEN")

DRIVE_TEMP_FOLDER_ID = os.getenv("GDRIVE_TEMP_FOLDER_ID")  # optional upload folder

SCOPES = ["https://www.googleapis.com/auth/drive"]
ONE_MB = 8 * 1024 * 1024  # 8 MiB chunk size for resumable uploads

try:
    from extensions import upload_progress_data  # optional global store
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Public helpers – upload / download / delete
# ---------------------------------------------------------------------------

def upload_to_gdrive_with_progress(
    source: Union[str, io.BytesIO, StreamReaderWrapper],
    filename_in_gdrive: str,
    operation_id_for_log: str,
) -> Generator[Dict[str, Any], None, Tuple[Optional[str], Optional[str]]]:
    """
    Upload *source* into your Drive and yield progress dictionaries.

    Yields:
        {"type": "progress", ...} dicts while uploading.

    Returns:
        (drive_file_id, error_message) as generator final value.
    """
    log_prefix = f"[GDriveUpload-{operation_id_for_log}-{filename_in_gdrive[:20]}]"

    try:
        service = _get_drive_service()
        file_metadata = {"name": filename_in_gdrive}
        if DRIVE_TEMP_FOLDER_ID:
            file_metadata["parents"] = [DRIVE_TEMP_FOLDER_ID]

        media_body: Optional[MediaIoBaseUpload] = None
        file_size = 0

        # Local file path -------------------------------------------------------
        if isinstance(source, str) and os.path.exists(source):
            file_size = os.path.getsize(source)
            media_body = MediaFileUpload(
                source,
                mimetype=mimetypes.guess_type(filename_in_gdrive)[0]
                or "application/octet-stream",
                resumable=True,
                chunksize=ONE_MB,
            )
        # In-memory buffer ------------------------------------------------------
        elif isinstance(source, io.BytesIO):
            source.seek(0, io.SEEK_END)
            file_size = source.tell()
            source.seek(0)
            media_body = MediaIoBaseUpload(
                fd=source,
                mimetype=mimetypes.guess_type(filename_in_gdrive)[0]
                or "application/octet-stream",
                resumable=True,
                chunksize=ONE_MB,
            )
        # Wrapped request stream -----------------------------------------------
        elif isinstance(source, StreamReaderWrapper):
            file_size = len(source)
            media_body = MediaIoBaseUpload(
                fd=source,
                mimetype=mimetypes.guess_type(filename_in_gdrive)[0]
                or "application/octet-stream",
                resumable=True,
                chunksize=ONE_MB,
            )
        else:
            err = ("Invalid source type for GDrive upload. Must be a file path, "
                   "io.BytesIO, or StreamReaderWrapper.")
            logging.error(f"{log_prefix} {err}")
            yield {"type": "error", "message": err}
            return None, err

        # Edge-case: zero-byte placeholder -------------------------------------
        if file_size == 0:
            empty = service.files().create(body=file_metadata, fields="id").execute()
            gid = empty.get("id")
            if gid:
                yield {"type": "progress", "percentage": 100, "bytes_sent": 0}
                return gid, None
            err = "Failed to create empty file placeholder."
            yield {"type": "error", "message": err}
            return None, err

        # Resumable upload loop -------------------------------------------------
        request = service.files().create(
            body=file_metadata,
            media_body=media_body,
            fields="id,name",
        )

        response = None
        last_pct = -1
        while response is None:
            try:
                status, response = request.next_chunk()
                if status:
                    pct = int(status.progress() * 100)
                    if pct > last_pct:
                        logging.info(f"{log_prefix} {pct}%")
                        yield {
                            "type": "progress",
                            "percentage": pct,
                            "bytes_sent": int(status.resumable_progress),
                        }
                        last_pct = pct
            except Exception as e:
                if 'client disconnected' in str(e).lower() or 'broken pipe' in str(e).lower():
                    err = "Client disconnected during upload"
                    logging.warning(f"{log_prefix} {err}")
                    yield {"type": "error", "message": err}
                    return None, err
                raise

        gid = response.get("id") if response else None
        if gid:
            logging.info(f"{log_prefix} Upload successful. ID: {gid}")
            return gid, None

        err = "Upload finished but no Drive ID returned."
        logging.error(f"{log_prefix} {err}")
        yield {"type": "error", "message": err}
        return None, err

    except HttpError as e:
        reason = getattr(e, "_get_reason", lambda: str(e))()
        err = f"Google Drive API HTTP error: {reason}"
        logging.error(f"{log_prefix} {err}")
        yield {"type": "error", "message": err}
        return None, err
    except Exception as e:
        err = f"Unexpected upload error: {str(e)}"
        logging.error(f"{log_prefix} {err}", exc_info=True)
        yield {"type": "error", "message": err}
        return None, err


# ---------------------------------------------------------------------------
# NEW HELPER – initiate resumable session
# ---------------------------------------------------------------------------

def initiate_resumable_gdrive_session(
    filename: str,
    filesize: int,
    mimetype: str
) -> Tuple[Optional[str], Optional[str]]:
    """
    Start a resumable-upload session and return the session URI.

    Args:
        filename:  Desired name in Drive.
        filesize:  Size in bytes.
        mimetype:  Mime-type string.

    Returns:
        (session_uri, error_message).  If error_message is None, success.
    """
    log_prefix = f"[GDrive-Resumable-Session-{filename[:20]}]"
    try:
        service = _get_drive_service()
    except Exception as e:
        err = f"Auth error obtaining Drive service: {e}"
        logging.error(f"{log_prefix} {err}")
        return None, err

    # Ensure we have an access token
    access_token: Optional[str] = getattr(service._http.credentials, "token", None)
    if not access_token:
        try:
            service._http.credentials.refresh(httplib2.Http())
            access_token = service._http.credentials.token
        except Exception as e:
            err = f"Could not refresh OAuth token: {e}"
            logging.error(f"{log_prefix} {err}")
            return None, err

    url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": mimetype,
        "X-Upload-Content-Length": str(filesize),
    }

    body = {"name": filename}
    if DRIVE_TEMP_FOLDER_ID:
        body["parents"] = [DRIVE_TEMP_FOLDER_ID]

    try:
        resp = requests.post(url, headers=headers, data=json.dumps(body), timeout=30)
    except requests.RequestException as e:
        err = f"Network error contacting Google Drive: {e}"
        logging.error(f"{log_prefix} {err}")
        return None, err

    if resp.status_code == 200:
        session_uri = resp.headers.get("Location")
        if session_uri:
            logging.info(f"{log_prefix} Session URI created.")
            return session_uri, None
        err = "Resumable session started but no Location header returned."
        logging.error(f"{log_prefix} {err}")
        return None, err

    err = (f"Google Drive resumable-session request failed "
           f"({resp.status_code}): {resp.text}")
    logging.error(f"{log_prefix} {err}")
    return None, err

# ---------------------------------------------------------------------------
# Other public helpers
# ---------------------------------------------------------------------------

def download_from_gdrive(gid: str) -> Tuple[Optional[io.BytesIO], Optional[str]]:
    """Download a file by ID from Drive into memory."""
    try:
        service = _get_drive_service()
        request = service.files().get_media(fileId=gid)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                logging.info(f"Download {gid}: {int(status.progress() * 100)}%")
        buf.seek(0)
        return buf, None
    except HttpError as e:
        if e.resp.status == 404:
            return None, f"File ID {gid} not found."
        return None, f"Drive HTTP error {e.resp.status}: {e._get_reason()}"
    except Exception as e:
        return None, f"Unexpected error downloading {gid}: {str(e)}"


def delete_from_gdrive(gid: str) -> Tuple[bool, Optional[str]]:
    """Delete file permanently."""
    try:
        service = _get_drive_service()
        service.files().delete(fileId=gid).execute()
        return True, None
    except HttpError as e:
        if e.resp.status == 404:
            return True, None  # already gone
        return False, f"Drive HTTP error {e.resp.status}: {e._get_reason()}"
    except Exception as e:
        return False, f"Unexpected delete error: {str(e)}"

# ---------------------------------------------------------------------------
# Private – OAuth credentials helper
# ---------------------------------------------------------------------------

def _get_drive_service():
    """Return a Drive service authenticated with user-refresh token."""
    if not (GDRIVE_CLIENT_ID and GDRIVE_CLIENT_SECRET and GDRIVE_REFRESH_TOKEN):
        raise ValueError("OAuth env vars missing (CLIENT_ID / SECRET / REFRESH_TOKEN).")

    creds = Credentials(
        token=None,
        refresh_token=GDRIVE_REFRESH_TOKEN,
        client_id=GDRIVE_CLIENT_ID,
        client_secret=GDRIVE_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )

    http = httplib2.Http(timeout=600)
    http.follow_redirects = False
    authed_http = AuthorizedHttp(creds, http=http)
    service = build("drive", "v3", http=authed_http, cache_discovery=False)
    return service


logging.info("Google Drive API module loaded – OAuth user mode.")

# ---------------------------------------------------------------------------
# Additional helper – direct download to file path
# ---------------------------------------------------------------------------

def download_from_gdrive_to_file(gid: str, destination_path: str) -> Tuple[bool, Optional[str]]:
    """Download Drive file directly to *destination_path*."""
    try:
        service = _get_drive_service()
        request = service.files().get_media(fileId=gid)

        with open(destination_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    logging.info(f"Download to file {gid}: {int(status.progress() * 100)}%")

        return True, None
    except HttpError as e:
        if e.resp.status == 404:
            return False, f"File ID {gid} not found."
        return False, f"Drive HTTP error {e.resp.status}: {e._get_reason()}"
    except Exception as e:
        return False, f"Unexpected error downloading {gid} to file: {str(e)}"
