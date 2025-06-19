# app_setup.py
import logging
import os
from flask_cors import CORS
from dotenv import load_dotenv
from flask_admin import Admin
from flask_login import LoginManager  # Keep this import
from flask import Flask, jsonify, send_from_directory

# Import your admin views
from routes.admin.user_admin_views import UserView
from routes.admin.file_admin_views import FileMetadataView
from routes.admin.dashboard_view import MyAdminIndexView
from routes.admin.archive_admin_views import ArchivedFileView
from routes.admin.archived_user_admin_views import ArchivedUserView

# Import the admin authentication blueprint
from routes.admin.auth_routes import admin_auth_bp

# Import User model and loader function components
from database import find_user_by_id_str, User

load_dotenv()

# --- App and Mail are imported from config.py ---
from config import app, mail, format_bytes, format_time, LOG_DIR
logging.info("app_setup.py: Started application setup.")

# ---------------------------------------------------------------------------
# Flask-Login setup
# ---------------------------------------------------------------------------
login_manager = LoginManager()
login_manager.init_app(app)  # attach to our imported Flask app


@login_manager.user_loader
def load_user(user_id_str):
    """Return a User object for Flask-Login sessions."""
    user_doc, _ = find_user_by_id_str(user_id_str)
    if user_doc:
        try:
            return User(user_doc)
        except ValueError as e:
            logger = app.logger if hasattr(app, "logger") else logging
            logger.error(
                f"Error creating User object for user_id {user_id_str} in user_loader: {e}"
            )
    return None


login_manager.login_view = "admin_auth.login"
login_manager.login_message = (
    "You must be logged in as an admin to access this page."
)
login_manager.login_message_category = "info"
logging.info("Flask-Login initialised and user_loader configured.")

# ---------------------------------------------------------------------------
# Flask-Admin setup
# ---------------------------------------------------------------------------
admin_dashboard_view = MyAdminIndexView(
    name="Dashboard", endpoint="admin", url="/admin"
)
admin = Admin(
    app, name="Storage Admin", template_mode="bootstrap4", url="/admin", index_view=admin_dashboard_view
)
logging.info("Flask-Admin initialised with custom dashboard.")

admin.add_view(
    UserView(
        name="Manage Users",
        endpoint="users",
        menu_icon_type="glyph",
        menu_icon_value="glyphicon-user",
    )
)
admin.add_view(
    FileMetadataView(
        name="File Uploads",
        endpoint="files",
        menu_icon_type="glyph",
        menu_icon_value="glyphicon-file",
    )
)
admin.add_view(
    ArchivedFileView(
        name="Archived Files",
        endpoint="archivedfiles",
        category="File Management",
        menu_icon_type="glyph",
        menu_icon_value="glyphicon-folder-open",
    )
)
admin.add_view(
    ArchivedUserView(
        name="Archived Users",
        endpoint="archivedusers",
        category="User Management",
        menu_icon_type="glyph",
        menu_icon_value="glyphicon-trash",
    )
)
logging.info("Flask-Admin views registered.")

# ---------------------------------------------------------------------------
# Jinja filters
# ---------------------------------------------------------------------------
app.jinja_env.filters["format_bytes"] = format_bytes
app.jinja_env.filters["format_time"] = format_time
logging.info("Custom Jinja filters registered.")

# ---------------------------------------------------------------------------
# CORS configuration  ←  **UPDATED HERE**
# ---------------------------------------------------------------------------
env_frontend_url_setting = os.environ.get("FRONTEND_URL")
allowed_origins_config = "*"
if env_frontend_url_setting:
    if env_frontend_url_setting.strip() == "*":
        allowed_origins_config = "*"
    else:
        allowed_origins_config = [
            url.strip() for url in env_frontend_url_setting.split(",") if url.strip()
        ]
else:
    logging.warning(
        "FRONTEND_URL environment variable not set. Defaulting CORS to allow all origins ('*')."
    )

CORS(
    app,
    origins=allowed_origins_config,
    supports_credentials=True,
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=[
        "Content-Type",
        "Authorization",
        "X-Requested-With",
        "X-Filesize",  # ← added so uploads with this header pass CORS pre-flight
    ],
    expose_headers=["Content-Disposition"],
)
logging.info(f"Flask-CORS initialised. Allowing origins: {allowed_origins_config}")

# ---------------------------------------------------------------------------
# Other extensions
# ---------------------------------------------------------------------------
from extensions import jwt

jwt.init_app(app)
logging.info("Flask-JWT-Extended initialised.")

# ---------------------------------------------------------------------------
# Blueprint registration
# ---------------------------------------------------------------------------
from routes.password_reset_routes import password_reset_bp
from routes.auth_routes import auth_bp
from routes.upload_routes import upload_bp
from routes.download_routes import download_bp as download_prefixed_bp, download_sse_bp
from routes.file_routes import file_bp
from routes.archive_routes import archive_bp

blueprints_to_register_with_prefix = {
    "password_reset": (password_reset_bp, None),
    "auth": (auth_bp, None),
    "upload": (upload_bp, "/upload"),
    "download_prefixed": (download_prefixed_bp, "/download"),
    "download_sse": (download_sse_bp, None),
    "file_routes": (file_bp, "/api"),
    "archive": (archive_bp, "/api/archive"),
}

registered_blueprints_count = 0
for name, (bp_instance, url_prefix) in blueprints_to_register_with_prefix.items():
    if bp_instance:
        if bp_instance.name in app.blueprints:
            logging.warning(
                f"Blueprint with internal name '{bp_instance.name}' (config key: '{name}') already registered. Skipping."
            )
        else:
            app.register_blueprint(bp_instance, url_prefix=url_prefix)
            logging.info(
                f"Blueprint '{bp_instance.name}' (config key: '{name}') registered with prefix: {url_prefix}"
            )
            registered_blueprints_count += 1
    else:
        logging.warning(
            f"Blueprint instance for config key '{name}' is None. Skipping registration."
        )

if registered_blueprints_count > 0:
    logging.info(
        f"Total of {registered_blueprints_count} user-defined blueprints registered via loop."
    )
else:
    logging.warning("No new user-defined blueprints were registered via loop.")

# Register the admin authentication blueprint
if "admin_auth" not in app.blueprints:
    app.register_blueprint(admin_auth_bp, url_prefix="/admin")
    logging.info(
        f"Admin authentication blueprint '{admin_auth_bp.name}' registered with prefix: /admin"
    )
else:
    logging.warning(
        f"Admin authentication blueprint '{admin_auth_bp.name}' appears to be already registered."
    )

# ---------------------------------------------------------------------------
# Root route
# ---------------------------------------------------------------------------
@app.route("/")
def home():
    return "whelcome to my site!"

# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app_env = os.environ.get("APP_ENV", "development").lower()
    is_dev = app_env == "development"
    log_level = logging.DEBUG if is_dev else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(levelname)s - %(name)s - %(module)s - %(message)s",
    )

    logging.info(f"Starting Flask server in '{app_env}' mode…")
    logging.info(f"  Debug mode: {is_dev}")
    logging.info(f"  Reloader:   {is_dev}")

    if not os.path.exists(LOG_DIR):
        try:
            os.makedirs(LOG_DIR)
            logging.info(f"Log directory created: {LOG_DIR}")
        except OSError as e:
            logging.error(f"Could not create logging directory {LOG_DIR}: {e}")

    app.run(
        host=os.environ.get("FLASK_RUN_HOST", "0.0.0.0"),
        port=int(os.environ.get("FLASK_RUN_PORT", 5000)),
        debug=is_dev,
        use_reloader=is_dev,
    )
