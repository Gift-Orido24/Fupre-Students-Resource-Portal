"""OAuth 2.0 Authentication module for Google Drive API."""

import json
import os
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from config import CONFIG_DIR, CREDENTIALS_FILE, SCOPES, TOKEN_FILE, log


class AuthError(Exception):
    """Raised when authentication cannot be completed."""


def _load_saved_credentials() -> Credentials | None:
    # On Render, the token is supplied through an environment variable.
    token_json = os.environ.get("GOOGLE_TOKEN_JSON")
    if token_json:
        try:
            return Credentials.from_authorized_user_info(
                json.loads(token_json), SCOPES
            )
        except (ValueError, KeyError) as exc:
            log.warning("Ignoring unreadable GOOGLE_TOKEN_JSON (%s).", exc)
            return None
    if not TOKEN_FILE.exists():
        return None
    try:
        return Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    except (ValueError, OSError) as exc:
        log.warning("Ignoring unreadable token file (%s).", exc)
        return None


def _save_credentials(creds: Credentials) -> None:
    """Save token credentials with owner-only read/write permissions (0600)."""
    if os.environ.get("GOOGLE_TOKEN_JSON"):
        return  # Hosted mode: the token lives in an environment variable.
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass


def get_credentials(open_browser: bool = False) -> Credentials:
    """Retrieves valid credentials, refreshing or requesting consent if needed."""
    creds = _load_saved_credentials()

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            log.info("Refreshing expired access token...")
            creds.refresh(Request())
            _save_credentials(creds)
            return creds
        except RefreshError:
            log.warning("Saved credentials revoked/expired; forcing re-authentication.")

    if os.environ.get("GOOGLE_TOKEN_JSON") or os.environ.get("RENDER"):
        raise AuthError(
            "Google token is missing or revoked. Generate a new token.json "
            "on your computer and update GOOGLE_TOKEN_JSON."
        )

    if not CREDENTIALS_FILE.exists():
        log.error("Missing credentials file at %s", CREDENTIALS_FILE)
        raise AuthError(
            f"Missing {CREDENTIALS_FILE}. Download an OAuth client file "
            "(Desktop app) from Google Cloud Console and save it there."
        )

    log.info("Starting fresh OAuth browser authentication flow...")
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(port=8080, open_browser=open_browser, access_type="offline", prompt="consent")
    _save_credentials(creds)
    return creds
if __name__ == "__main__":
    get_credentials()


