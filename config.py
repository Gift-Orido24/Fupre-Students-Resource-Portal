"""Configuration settings, paths, and shared logging setup."""

import logging
import os
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

CONFIG_DIR = Path(
    os.environ.get("DRIVE_UPLOADER_HOME", Path(__file__).parent)
).expanduser()
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"
TOKEN_FILE = CONFIG_DIR / "token.json"
DB_FILE = Path("database.sqlite")

CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB chunks for resumable uploads
MAX_RETRIES = 5
FOLDER_MIME = "application/vnd.google-apps.folder"

# Configure shared logging handler
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("app.log"),
        logging.StreamHandler(sys.stdout)
    ]
)

log = logging.getLogger("drive_portal")

