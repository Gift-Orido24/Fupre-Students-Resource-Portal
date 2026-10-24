# FUPRE Student Resource Portal

A lightweight web portal where students can search for course materials by course title or course code and download them. Administrators upload files through a password-protected page. Files are stored in Google Drive, and their metadata (title, subject, size, hash) is stored in a SQLite-compatible database.

## Features

**For students**
- Search by course title or course code, with 20 results per page
- Download files directly from the portal; students never need a Google account or a Drive link
- Mobile-friendly interface
- Contact links (WhatsApp) for requests and support

**For administrators**
- Password-protected admin page (`/admin/login`) with a 15-minute session timeout
- Upload files with a title and subject (up to 80 MB per file)
- Duplicate protection: every file is hashed with SHA-256, and a file that already exists is rejected before it reaches Drive
- Automatic cleanup: if an upload fails after the file reached Drive, the orphaned Drive file is removed
- CSRF protection on all forms

## How it works

```
Student browser ──▶ Flask app (Render) ──▶ Turso database   (file metadata)
                         │
                         └──────────────▶ Google Drive       (the files themselves)
```

1. An admin uploads a file. The app hashes it, checks for duplicates, uploads it to Google Drive, and saves a record in the database.
2. A student searches. The app queries the database and shows matching records.
3. A student downloads. The app fetches the file from Drive and streams it to the student, so Drive links are never exposed.

## Tech stack

- **Backend:** Python, Flask, Gunicorn
- **File storage:** Google Drive API (OAuth 2.0)
- **Database:** SQLite locally, [Turso](https://turso.tech) (hosted SQLite) in production
- **Hosting:** Render (free web service)

## Project structure

| File | Purpose |
|---|---|
| `app.py` | Flask app: routes, templates, admin login, upload and download |
| `database.py` | Database access; uses Turso when configured, local SQLite otherwise |
| `auth.py` | Google OAuth: loads, refreshes and saves credentials |
| `drive_service.py` | Google Drive helpers (upload, folders, retries) |
| `config.py` | Paths, constants and logging setup |
| `source.py` | Command-line entry point for running the server and uploading files |
| `requirements.txt` | Python dependencies |

## Configuration

Settings are read from environment variables. Locally, put them in a `.env` file (never commit it). On Render, set them in the **Environment** tab.

| Variable | Required | Description |
|---|---|---|
| `FLASK_SECRET_KEY` | Yes | Long random string used to sign sessions |
| `PORTAL_ADMIN_PASSWORD` | Yes | Password for the admin page; use a long one |
| `TURSO_DATABASE_URL` | Production | Turso database URL (`libsql://...`) |
| `TURSO_AUTH_TOKEN` | Production | Turso token with read and write access |
| `GOOGLE_TOKEN_JSON` | Production | Full contents of `token.json`, as one line |
| `PYTHON_VERSION` | Render | Python version for the build, e.g. `3.12.3` |
| `DRIVE_UPLOADER_HOME` | No | Folder holding credentials and local data (defaults to the project folder) |

If the two Turso variables are not set, the app uses a local `database.sqlite` file instead.

## Running locally

1. Clone the repo and install dependencies:
   ```
   pip install -r requirements.txt
   ```
2. Set up Google Drive access:
   - In Google Cloud Console, create a project and enable the **Google Drive API**.
   - Create an **OAuth client ID** (Desktop app) and download it as `credentials.json` into the project folder.
   - Run `python auth.py` and approve access in the browser. This creates `token.json`.
3. Create a `.env` file:
   ```
   FLASK_SECRET_KEY=your-long-random-string
   PORTAL_ADMIN_PASSWORD=your-admin-password
   ```
4. Start the server:
   ```
   python source.py serve --port 5000
   ```
5. search for the `http//...` link and open it on your browser. The admin login is at `/admin/login`.

### Uploading from the command line

```
python source.py upload file1.pdf file2.pdf
python source.py upload notes.pdf --folder-name "GET 205"
```

## Deploying (Render + Turso)

1. **Database:** create a Turso database, upload your SQLite file, then copy the database URL and generate a read and write token. Turso only accepts uploads in WAL mode, so convert a copy of your database first:
   ```
   cp database.sqlite database_upload.sqlite
   python -c "import sqlite3,sys; c=sqlite3.connect('file:'+sys.argv[1]+'?mode=rw',uri=True); print(c.execute('PRAGMA journal_mode=WAL').fetchone()); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); print(c.execute('SELECT COUNT(*) FROM files').fetchone()); c.close()" database_upload.sqlite
   ```
   It should print `('wal',)` followed by your record count. Upload `database_upload.sqlite` to Turso.
2. **Repository:** push this project to GitHub. Make sure `.gitignore` excludes `.env`, `token.json`, `credentials.json` and any `.sqlite` files.
3. **Render:** create a **Web Service** from the repo with:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app --timeout 120`
   - Instance type: Free
4. **Environment:** add the variables from the table above. For `GOOGLE_TOKEN_JSON`, paste the contents of `token.json` on a single line.
5. Deploy and check the **Logs** tab.

Render deploys automatically on every push to `main`.

## Security notes

- Secrets live only in environment variables, never in the repository.
- Admin actions require login and a per-session CSRF token.
- Uploads are validated (non-empty, safe filenames, size limit, no duplicates).
- Use a long, unique admin password. This login is a single shared password.

## Limitations

- **Free-tier sleep:** the Render free plan sleeps after 15 minutes without traffic, so the first request afterward can take around a minute.
- **Google token:** if the Google Cloud consent screen is in "Testing" mode, the token expires after about 7 days. Publish the app to production to avoid this. If the token is ever revoked, run `python auth.py` locally and update `GOOGLE_TOKEN_JSON`.
- **Free database limits:** Turso's free plan has monthly usage limits. A portal of this size should stay well within them.
- **Backups:** keep a regular export of the database. Free-tier terms can change.

## Contributing

Issues and pull requests are welcome. Please never include credentials, tokens or database files in a contribution.

