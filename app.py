"""Flask Web Application for the Student Learning Portal."""

from urllib.parse import quote
from flask import Response, stream_with_context
from google.auth.transport.requests import AuthorizedSession
from auth import get_credentials
import hashlib
import os
import secrets
import shutil
import re
import mimetypes
import sqlite3
import tempfile
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template_string,
    request,
    send_file,
    session,
    url_for,
)
from datetime import timedelta

from googleapiclient.http import MediaIoBaseDownload
from werkzeug.utils import secure_filename

from database import get_db_connection
from drive_service import get_drive_service, upload_file_to_drive
from config import log



# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

app = Flask(__name__)

app.secret_key = os.environ.get("FLASK_SECRET_KEY")
app.permanent_session_lifetime = timedelta(minutes=15)
ADMIN_PASSWORD = os.environ.get("PORTAL_ADMIN_PASSWORD")

MAX_UPLOAD_MB = 80
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

ITEMS_PER_PAGE = 20

_ready = False


# --------------------------------------------------
# UTILITIES
# --------------------------------------------------

def human_size(n):
    n = float(n or 0)

    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024

    return f"{n:.1f} TB"


app.jinja_env.filters["human_size"] = human_size


def calculate_sha256(file_path):
    """Calculate a file's SHA-256 hash without loading it all into memory."""
    digest = hashlib.sha256()

    with open(file_path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def is_admin():
    return session.get("admin") is True


def get_csrf_token():
    """Generate or retrieve a CSRF token for the current session."""
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = get_csrf_token


def verify_csrf():
    """Verify the submitted CSRF token matches the session token."""
    token = request.form.get("csrf_token")
    if not token or token != session.get("csrf_token"):
        abort(400, description="Invalid or missing CSRF token.")


def download_drive_file(service, drive_id, destination):
    """Download a Google Drive file into a local file."""
    metadata = service.files().get(
        fileId=drive_id,
        fields="id,name,mimeType,size",
    ).execute()

    request_media = service.files().get_media(fileId=drive_id)

    with open(destination, "wb") as output:
        downloader = MediaIoBaseDownload(
            output,
            request_media,
            chunksize=1024 * 1024,
        )

        done = False

        while not done:
            _, done = downloader.next_chunk()

    return metadata


# --------------------------------------------------
# DATABASE MIGRATION
# --------------------------------------------------

def migrate_db():
    """Add the size and file_hash columns, and a unique index on the hash."""
    conn = get_db_connection()

    try:
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(files)").fetchall()
        }

        if not cols:
            raise RuntimeError(
                "The files table does not exist. Initialize the database first."
            )

        if "size" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN size INTEGER")

        if "file_hash" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN file_hash TEXT")

        conn.commit()

        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_files_unique_hash
            ON files(file_hash)
            WHERE file_hash IS NOT NULL
        """)

        conn.commit()

    finally:
        conn.close()


@app.before_request
def prepare():
    """Check configuration and migrate the database once per start."""
    global _ready

    if _ready:
        return None

    if not app.secret_key or not ADMIN_PASSWORD:
        log.error(
            "FLASK_SECRET_KEY and PORTAL_ADMIN_PASSWORD must be set."
        )
        return (
            "Server is not configured. Set FLASK_SECRET_KEY and "
            "PORTAL_ADMIN_PASSWORD, then restart.",
            500,
        )

    migrate_db()
    _ready = True
    return None


@app.errorhandler(413)
def file_too_large(_error):
    return (
        f"File too large. The limit is {MAX_UPLOAD_MB} MB. "
        "Go back and choose a smaller file.",
        413,
    )


# --------------------------------------------------
# BACKFILL EXISTING FILE HASHES (run manually)
# --------------------------------------------------

def backfill_missing_hashes():
    """Calculate hashes and sizes for old records that lack them safely."""
    migrate_db()
    service = get_drive_service()

    conn = get_db_connection()

    try:
        records = conn.execute("""
            SELECT id, drive_file_id
            FROM files
            WHERE file_hash IS NULL OR file_hash = ''
        """).fetchall()
    finally:
        conn.close()

    updated = 0

    for record in records:
        drive_id = record["drive_file_id"]
        temp_dir = tempfile.mkdtemp()
        temp_path = os.path.join(temp_dir, "temp_backfill")

        try:
            download_drive_file(service, drive_id, temp_path)
            file_hash = calculate_sha256(temp_path)
            size = os.path.getsize(temp_path)

            conn = get_db_connection()

            try:
                cur = conn.execute("""
                    UPDATE files
                    SET file_hash = ?, size = ?
                    WHERE id = ?
                      AND (file_hash IS NULL OR file_hash = '')
                """, (file_hash, size, record["id"]))

                conn.commit()
                updated += 1

            except sqlite3.IntegrityError:
                conn.rollback()

                log.warning(
                    "Existing duplicate detected during backfill: "
                    "database record %s, Drive ID %s",
                    record["id"],
                    drive_id,
                )

            finally:
                conn.close()

        except Exception as exc:
            log.error(
                "Could not backfill hash for Drive ID %s: %s",
                drive_id,
                exc,
            )

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    log.info("Hash backfill completed. Records updated: %s", updated)


# --------------------------------------------------
# STYLING
# --------------------------------------------------

STYLE = """
body {
    font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
    background: #f4f6f9;
    padding: 40px 20px;
}
.container {
    max-width: 800px;
    margin: 0 auto;
    background: white;
    padding: 30px;
    border-radius: 10px;
    box-shadow: 0 4px 6px rgba(0,0,0,0.05);
}
h1, h2 { color: #2c3e50; }
.search-box, .upload-box {
    display: flex;
    gap: 10px;
    margin-bottom: 20px;
}
.search-area { margin-top: 10px; }
.search-hint {
    display: block;
    margin-bottom: 12px;
    font-size: 15px;
    line-height: 1.45;
    color: #7f8c8d;
}
.empty { margin: 22px 0 6px; color: #7f8c8d; }
.recent-title { font-size: 18px; margin: 24px 0 4px; }
input {
    flex: 1;
    padding: 10px;
    border: 1px solid #ccc;
    border-radius: 6px;
}
input::placeholder { color: #95a5a6; opacity: 1; }
input:focus-visible, button:focus-visible {
    outline: 2px solid #3498db;
    outline-offset: 2px;
}
.show-password {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 14px;
    color: #7f8c8d;
    cursor: pointer;
}
.show-password input { flex: none; width: auto; }
button {
    padding: 10px 20px;
    background: #3498db;
    color: white;
    border: none;
    border-radius: 6px;
    cursor: pointer;
}
.btn-download {
    background: #2ecc71;
    color: white;
    text-decoration: none;
    padding: 8px 16px;
    border-radius: 6px;
    font-weight: bold;
}
.file-card {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    padding: 15px;
    border-bottom: 1px solid #eee;
}
.badge {
    background: #e8f4fd;
    color: #2980b9;
    padding: 3px 8px;
    border-radius: 4px;
    font-size: 12px;
}
.size {
    color: #7f8c8d;
    font-size: 12px;
    margin-right: 10px;
}
.flash {
    background: #d4edda;
    color: #155724;
    padding: 10px;
    margin-bottom: 15px;
    border-radius: 6px;
}
.pagination {
    margin-top: 20px;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.pagination a {
    padding: 8px 14px;
    background: #3498db;
    color: white;
    text-decoration: none;
    border-radius: 6px;
}
.file-card > div:last-child {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 6px;
    flex-shrink: 0;
}
.size { margin-right: 0; }
.file-card h3 { word-break: break-word; }
.search-box input, .upload-box input { min-width: 0; }
.search-box button, .upload-box button { flex-shrink: 0; }
.site-footer {
    max-width: 800px;
    margin: 20px auto 0;
    padding: 24px 30px;
    background: #0b1f4d;
    color: #e5e7eb;
    text-align: center;
    font-size: 14px;
    line-height: 1.5;
    border-radius: 10px;
}
.site-footer h3 { margin: 0 0 8px; color: white; }
.site-footer p { margin: 4px 0; }
.site-footer a { color: #93c5fd; text-decoration: none; }
.site-footer a:hover { text-decoration: underline; }
.contact-list { display: flex; flex-direction: column; align-items: center; gap: 8px; margin-top: 10px; }
.contact-list a { display: inline-flex; align-items: center; gap: 8px; font-size: 15px; }
.wa-icon { width: 20px; height: 20px; fill: #25D366; flex-shrink: 0; }
body {
    min-height: 100vh;
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
}
.container {
    width: 100%;
    margin: 0 auto 20px;
}
.site-footer {
    width: 100%;
    margin: auto auto 0;
}   
@media (max-width: 600px) {
    body { padding: 15px 8px; }
    .container { padding: 18px; }
    .site-footer { padding: 18px; }
}
"""

FLASH_BLOCK = """
{% with messages = get_flashed_messages() %}
  {% for message in messages %}
    <div class="flash">{{ message }}</div>
  {% endfor %}
{% endwith %}
"""


# --------------------------------------------------
# TEMPLATES
# --------------------------------------------------

PUBLIC_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fupre Student Resource Portal</title>
<style>{{ style|safe }}</style>
</head>
<body>
<div class="container">
<h1>📚 Course Materials Search</h1>
""" + FLASH_BLOCK + """
<form class="search-area" method="GET" action="/" role="search">
  <label class="search-hint" for="search-query">
    Search for materials by course title or course code.
  </label>
  <div class="search-box">
    <input id="search-query" type="search" name="query"
           placeholder="e.g. Fluid or GET 205"
           value="{{ query }}" autocomplete="off" enterkeyhint="search">
    <button type="submit">Search</button>
  </div>
</form>
{% if not query and recent_files %}
<h2 class="recent-title">🆕 Recently added</h2>
{% for file in recent_files %}
<div class="file-card">
  <div>
    <h3 style="margin: 0 0 5px 0;">{{ file.title }}</h3>
    <span class="badge">{{ file.subject }}</span>
  </div>
  <div>
    {% if file.size %}
      <span class="size">{{ file.size | human_size }}</span>
    {% endif %}
    <a href="/download/{{ file.id }}" class="btn-download">Download</a>
  </div>
</div>
{% endfor %}
{% endif %}

<div class="file-list">
{% if query %}
  {% if files %}
    {% for file in files %}
    <div class="file-card">
      <div>
        <h3 style="margin: 0 0 5px 0;">{{ file.title }}</h3>
        <span class="badge">{{ file.subject }}</span>
      </div>
      <div>
        {% if file.size %}
          <span class="size">{{ file.size | human_size }}</span>
        {% endif %}
        <a href="/download/{{ file.id }}" class="btn-download">Download</a>
      </div>
    </div>
    {% endfor %}
  {% else %}
    <p class="empty">Course material not available.</p>
  {% endif %}
{% endif %}
</div>

{% if total_pages > 1 %}
<div class="pagination">
  {% if page > 1 %}
    <a href="/?query={{ query|urlencode }}&amp;page={{ page - 1 }}">Previous</a>
  {% else %}
    <div></div>
  {% endif %}
  <span>Page {{ page }} of {{ total_pages }}</span>
  {% if page < total_pages %}
    <a href="/?query={{ query|urlencode }}&amp;page={{ page + 1 }}">Next</a>
  {% else %}
    <div></div>
  {% endif %}
</div>
{% endif %}

</div>
<footer class="site-footer">
  <h3>We apologise if you were unable to find your course material</h3>
  <p>for inqury, support or additions, contact us @</p>
  <div class="contact-list">
    <a href="https://wa.me/2348102952284" target="_blank" rel="noopener">
      <svg class="wa-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413Z"/></svg>
      08102952284
    </a>
    <a href="https://wa.me/2349129777078" target="_blank" rel="noopener">
      <svg class="wa-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413Z"/></svg>
      09129777078
    </a>
    <a href="https://wa.me/2348074568342" target="_blank" rel="noopener">
      <svg class="wa-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413Z"/></svg>
      08074568342
    </a>
  </div>
</footer>
</body>
</html>
"""


LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin Login</title>
<style>{{ style|safe }}</style>
</head>
<body>
<div class="container">
<h2>🔒 Admin Login</h2>
""" + FLASH_BLOCK + """
<form class="upload-box" method="POST"
      style="flex-direction: column;">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <input type="password" id="password" name="password"
         placeholder="Admin password" required>
  <label class="show-password">
    <input type="checkbox" id="toggle-password"> Show password
  </label>
  <button type="submit">Log in</button>
</form>

<script>
  document.getElementById("toggle-password").addEventListener("change", function () {
    document.getElementById("password").type = this.checked ? "text" : "password";
  });
</script>
</div>
</body>
</html>
"""


ADMIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin Upload</title>
<style>{{ style|safe }}</style>
</head>
<body>
<div class="container">
<h2>📤 Admin Upload</h2>
""" + FLASH_BLOCK + """
<form class="upload-box" method="POST"
      action="/admin/upload" enctype="multipart/form-data"
      style="flex-direction: column;">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <input type="text" name="title"
         placeholder="Document Title" required>
  <input type="text" name="subject"
         placeholder="Subject" required>
  <input type="file" name="file" required>
  <button type="submit" style="background: #27ae60;">
    Upload to Drive & Publish
  </button>
</form>
<p><a href="/">Back to portal</a></p>
<form method="POST" action="/admin/logout">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <button type="submit">Log out</button>
</form>
</div>
<script>
  setTimeout(function () {
    window.location.href = "/admin/login";
  }, 15 * 60 * 1000 + 5000);
</script>
</body>
</html>
"""
# --------------------------------------------------
# PUBLIC RESOURCE LIST WITH PAGINATION
# --------------------------------------------------

@app.route("/")
def index():
    query = request.args.get("query", "").strip()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    offset = (page - 1) * ITEMS_PER_PAGE
    files = []
    recent_files = []
    total_items = 0

    # Only query the database if the user has typed a search query
    if query:
        conn = get_db_connection()
        try:
            count_row = conn.execute("""
                SELECT COUNT(*) AS total
                FROM files
                WHERE title LIKE ? OR subject LIKE ?
            """, (f"%{query}%", f"%{query}%")).fetchone()
            total_items = count_row["total"] if count_row else 0

            files = conn.execute("""
                SELECT *
                FROM files
                WHERE title LIKE ? OR subject LIKE ?
                ORDER BY id DESC
                LIMIT ? OFFSET ?
            """, (f"%{query}%", f"%{query}%", ITEMS_PER_PAGE, offset)).fetchall()
        finally:
            conn.close()
    else:
        conn = get_db_connection()
        try:
            recent_files = conn.execute("""
                SELECT * 
                FROM files 
                ORDER BY id DESC 
                LIMIT 6
        """).fetchall()
        finally:
            conn.close()


    total_pages = max(1, (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)

    return render_template_string(
        PUBLIC_TEMPLATE,
        files=files,
        recent_files=recent_files,
        query=query,
        page=page,
        total_pages=total_pages,
        style=STYLE,
    )

# --------------------------------------------------
# DOWNLOAD RESOURCE
# --------------------------------------------------
@app.route("/download/<int:file_id>")
def download_file(file_id):
    conn = get_db_connection()

    try:
        record = conn.execute(
            "SELECT * FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()
    finally:
        conn.close()

    if not record:
        abort(404, description="File record not found.")

    drive_id = record["drive_file_id"]

    try:
        service = get_drive_service()
        meta = service.files().get(
            fileId=drive_id,
            fields="name,mimeType,size",
        ).execute()

        authed = AuthorizedSession(get_credentials(open_browser=False))
        upstream = authed.get(
            f"https://www.googleapis.com/drive/v3/files/{drive_id}",
            params={"alt": "media"},
            stream=True,
        )

        if upstream.status_code != 200:
            upstream.close()
            raise RuntimeError(f"Drive returned {upstream.status_code}")

    except Exception as exc:
        log.error("Download failed for Drive ID %s: %s", drive_id, exc)
        abort(502, description="Could not fetch the file from Drive.")

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=256 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    designation = record["title"]  # your real column name

    ext = os.path.splitext(meta["name"])[1] or (
        mimetypes.guess_extension(meta.get("mimeType", "")) or ""
    )
    safe = re.sub(r'[\\/:*?"<>|\r\n]', "_", designation).strip() or "download"
    download_name = f"{safe}{ext}"

    headers = {
        "Content-Disposition":
            f"attachment; filename*=UTF-8''{quote(download_name)}",
    }

    if meta.get("size"):
        headers["Content-Length"] = meta["size"]

    return Response(
        stream_with_context(generate()),
        mimetype=meta.get("mimeType", "application/octet-stream"),
        headers=headers,
    )
# --------------------------------------------------
# ADMIN LOGIN & LOGOUT
# --------------------------------------------------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        verify_csrf()
        if request.form.get("password") == ADMIN_PASSWORD:
            session.clear()
            session.permanent = True
            session["admin"] = True
            get_csrf_token()

            return redirect(url_for("admin_page"))

        flash("Wrong password.")

    return render_template_string(LOGIN_TEMPLATE, style=STYLE)


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    verify_csrf()
    session.clear()
    return redirect(url_for("index"))


@app.route("/admin")
def admin_page():
    if not is_admin():
        return redirect(url_for("admin_login"))

    return render_template_string(ADMIN_TEMPLATE, style=STYLE)


# --------------------------------------------------
# ADMIN UPLOAD (Safe cleanup & CSRF protected)
# --------------------------------------------------

@app.route("/admin/upload", methods=["POST"])
def admin_upload():
    if not is_admin():
        abort(403)

    verify_csrf()

    title = request.form.get("title", "").strip()
    subject = request.form.get("subject", "").strip()
    uploaded_file = request.files.get("file")

    if not title or not subject:
        flash("Title and subject are required.")
        return redirect(url_for("admin_page"))

    if not uploaded_file or not uploaded_file.filename:
        flash("No file selected.")
        return redirect(url_for("admin_page"))

    filename = secure_filename(uploaded_file.filename)

    if not filename:
        flash("Invalid filename.")
        return redirect(url_for("admin_page"))

    temp_dir = tempfile.mkdtemp()
    temp_path = os.path.join(temp_dir, filename)
    drive_id = None

    try:
        uploaded_file.save(temp_path)

        file_hash = calculate_sha256(temp_path)
        size = os.path.getsize(temp_path)

        if size == 0:
            flash("Empty files cannot be uploaded.")
            return redirect(url_for("admin_page"))

        # Fast duplicate check before contacting Google Drive
        conn = get_db_connection()

        try:
            duplicate = conn.execute("""
                SELECT title
                FROM files
                WHERE file_hash = ?
                LIMIT 1
            """, (file_hash,)).fetchone()
        finally:
            conn.close()

        if duplicate:
            flash(
                f"Duplicate: this file already exists as "
                f"'{duplicate['title']}'."
            )
            return redirect(url_for("admin_page"))

        # Upload to Drive
        service = get_drive_service()
        drive_result = upload_file_to_drive(service, Path(temp_path))
        drive_id = drive_result["id"]

        # Store record in transaction with atomic recheck
        conn = get_db_connection()

        try:
            duplicate = conn.execute("""
                SELECT title
                FROM files
                WHERE file_hash = ?
                LIMIT 1
            """, (file_hash,)).fetchone()

            if duplicate:
                conn.rollback()

                flash(
                    f"Duplicate: this file already exists as "
                    f"'{duplicate['title']}'."
                )

                try:
                    service.files().delete(fileId=drive_id).execute()
                except Exception as cleanup_error:
                    log.error(
                        "Could not remove duplicate Drive file %s: %s",
                        drive_id,
                        cleanup_error,
                    )

                return redirect(url_for("admin_page"))

            conn.execute("""
                INSERT INTO files
                    (title, subject, drive_file_id, size, file_hash)
                VALUES (?, ?, ?, ?, ?)
            """, (title, subject, drive_id, size, file_hash))

            conn.commit()

        except Exception:
            conn.rollback()
            raise

        finally:
            conn.close()

        flash(f"Successfully uploaded '{title}'!")

        log.info(
            "Admin upload succeeded: %s -> Drive ID %s",
            title,
            drive_id,
        )

    except sqlite3.IntegrityError:
        flash("Duplicate detected. The file was not added to the database.")

        if drive_id:
            try:
                get_drive_service().files().delete(
                    fileId=drive_id
                ).execute()
            except Exception as cleanup_error:
                log.error(
                    "Could not clean up Drive ID %s after database conflict: %s",
                    drive_id,
                    cleanup_error,
                )

    except Exception as exc:
        log.error("Failed web upload process: %s", exc)

        flash("Upload failed. Check the application logs for details.")

        if drive_id:
            try:
                get_drive_service().files().delete(
                    fileId=drive_id
                ).execute()
            except Exception as cleanup_error:
                log.error(
                    "Could not clean up orphaned Drive ID %s: %s",
                    drive_id,
                    cleanup_error,
                )

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    return redirect(url_for("admin_page"))


# --------------------------------------------------
# RUN
# --------------------------------------------------

if __name__ == "__main__":
    app.run(debug=False)


