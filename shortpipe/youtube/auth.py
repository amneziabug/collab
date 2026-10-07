"""YouTube OAuth 2.0 (installed-app flow) with token caching and refresh.

Setup (once):
  1. Google Cloud Console -> create a project -> enable "YouTube Data API v3".
  2. OAuth consent screen: External, add your Google account as a *test user*.
  3. Credentials -> Create OAuth client ID -> "Desktop app" -> download JSON to
     secrets/client_secret.json.
  4. Run `python -m shortpipe auth` and approve in the browser.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# Upload-only scope: the app cannot delete or edit anything else on the channel.
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def get_credentials(client_secrets: Path, token_file: Path, *, interactive: bool = True,
                    port: int = 8080, open_browser: bool = True):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save(creds, token_file)
            log.info("Refreshed YouTube access token")
            return creds
        except RefreshError as exc:
            # Refresh tokens of apps in "Testing" status expire after 7 days.
            log.warning("Token refresh failed (%s); re-authorization required", exc)
            creds = None

    if not interactive:
        from .uploader import YouTubeAuthError
        raise YouTubeAuthError("YouTube credentials missing or expired; run `python -m shortpipe auth`")
    if not client_secrets.exists():
        raise FileNotFoundError(f"OAuth client file not found: {client_secrets}")

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
    creds = flow.run_local_server(port=port, open_browser=open_browser,
                                  access_type="offline", prompt="consent")
    _save(creds, token_file)
    log.info("Saved YouTube credentials to %s", token_file)
    return creds


def _save(creds, token_file: Path) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(token_file, 0o600)
    except OSError:
        pass
