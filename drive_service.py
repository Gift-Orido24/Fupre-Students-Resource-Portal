"""Google Drive API operational functions."""

import mimetypes
from pathlib import Path
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from auth import get_credentials
from config import CHUNK_SIZE, FOLDER_MIME, MAX_RETRIES, log


def get_drive_service(open_browser: bool = True):
    """Builds and returns an authorized Google Drive API client."""
    creds = get_credentials(open_browser=open_browser)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_or_create_folder(service, name: str, parent_id: str | None = None) -> str:
    """Finds or creates a folder in Google Drive."""
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    query = f"mimeType='{FOLDER_MIME}' and name='{escaped}' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"

    found = (
        service.files()
        .list(q=query, spaces="drive", fields="files(id, name)", pageSize=1)
        .execute(num_retries=MAX_RETRIES)
        .get("files", [])
    )
    if found:
        return found[0]["id"]

    metadata = {"name": name, "mimeType": FOLDER_MIME}
    if parent_id:
        metadata["parents"] = [parent_id]

    folder = (
        service.files()
        .create(body=metadata, fields="id")
        .execute(num_retries=MAX_RETRIES)
    )
    log.info("Created folder '%s' (ID: %s)", name, folder["id"])
    return folder["id"]


def upload_file_to_drive(service, file_path: str | Path, folder_id: str | None = None) -> dict:
    """Uploads a local file to Google Drive using resumable chunking."""
    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    size = path.stat().st_size
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    metadata: dict = {"name": path.name}
    if folder_id:
        metadata["parents"] = [folder_id]

    resumable = size > 0
    media = MediaFileUpload(
        str(path), mimetype=mime_type, chunksize=CHUNK_SIZE, resumable=resumable
    )
    request = service.files().create(
    body=metadata, media_body=media, fields="id, name, size, webViewLink")

    if not resumable:
        return request.execute(num_retries=MAX_RETRIES)

    response = None
    last_pct = -1
    while response is None:
        status, response = request.next_chunk(num_retries=MAX_RETRIES)
        if status:
            pct = int(status.progress() * 100)
            if pct != last_pct:
                log.info("%s: %d%% uploaded", path.name, pct)
                last_pct = pct
    return response

