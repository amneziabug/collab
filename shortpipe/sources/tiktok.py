"""Your own TikTok videos, via TikTok's official Display API (Login Kit OAuth).

What this source does:
  * logs in to *your* TikTok account (scopes user.info.basic, video.list),
  * lists *your* posted videos with their view/like/comment/share counts,
  * pairs each one with a local copy of the file you exported yourself
    (TikTok app -> Settings -> Account -> Download your data, or "Save video"
    on your own post), placed in `tiktok.video_dir` as `<video_id>.mp4`.

The Display API only returns the logged-in user's own videos, so every item is
`license = "owned"` by construction. It does not fetch, scrape or download other
people's videos.

Setup: create an app at https://developers.tiktok.com, add Login Kit + the
video.list scope, register the redirect URI http://localhost:<redirect_port>/callback/
for Desktop, and add your account as a sandbox target user.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import logging
import re
import secrets as pysecrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Callable, Iterator

from .base import SourceItem, VideoSource

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
USER_INFO_URL = "https://open.tiktokapis.com/v2/user/info/"
VIDEO_LIST_URL = "https://open.tiktokapis.com/v2/video/list/"
SCOPES = "user.info.basic,video.list"
VIDEO_FIELDS = ("id,title,video_description,duration,create_time,share_url,"
                "view_count,like_count,comment_count,share_count")
VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm")


class TikTokError(RuntimeError):
    """A TikTok API call failed."""


class TikTokAuthError(TikTokError):
    """Missing, expired or rejected TikTok login: run `python -m shortpipe tiktok-auth`."""


# ---------------------------------------------------------------------------
# HTTP


def _http(method: str, url: str, *, data: dict | None = None, json_body: dict | None = None,
          headers: dict | None = None, timeout: int = 30) -> tuple[int, dict]:
    headers = dict(headers or {})
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TikTokError(f"could not reach TikTok ({exc})") from exc


HttpFn = Callable[..., tuple[int, dict]]


# ---------------------------------------------------------------------------
# OAuth (Login Kit for Desktop, PKCE)


def pkce_pair() -> tuple[str, str]:
    """TikTok's desktop flow uses the *hex* SHA-256 of the verifier as the challenge."""
    verifier = pysecrets.token_urlsafe(64)[:96]
    challenge = hashlib.sha256(verifier.encode()).hexdigest()
    return verifier, challenge


class TikTokAuth:
    def __init__(self, client_key: str, client_secret: str, token_file: Path,
                 redirect_port: int = 8765, http: HttpFn = _http):
        if not client_key or not client_secret:
            raise TikTokAuthError("TikTok client key/secret missing: set TIKTOK_CLIENT_KEY and "
                                  "TIKTOK_CLIENT_SECRET (from your app on developers.tiktok.com)")
        self.client_key = client_key
        self.client_secret = client_secret
        self.token_file = Path(token_file)
        self.redirect_uri = f"http://localhost:{redirect_port}/callback/"
        self.redirect_port = redirect_port
        self._http = http

    # -- interactive login ----------------------------------------------------
    def login(self, open_browser: bool = True, timeout: int = 300) -> dict:
        verifier, challenge = pkce_pair()
        state = pysecrets.token_urlsafe(16)
        url = AUTHORIZE_URL + "?" + urllib.parse.urlencode({
            "client_key": self.client_key, "scope": SCOPES, "response_type": "code",
            "redirect_uri": self.redirect_uri, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        result: dict = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                result.update({k: v[0] for k, v in query.items()})
                ok = "code" in result and result.get("state") == state
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"TikTok login complete. You can close this tab."
                                 if ok else b"TikTok login failed. Check the terminal.")

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", self.redirect_port), Handler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        print(f"Open this URL to log in to TikTok:\n{url}\n")
        if open_browser:
            webbrowser.open(url)
        thread.join(timeout)
        server.server_close()

        if result.get("state") != state:
            raise TikTokAuthError("TikTok login did not complete (no callback or state mismatch)")
        if "code" not in result:
            raise TikTokAuthError(f"TikTok login refused: {result.get('error_description') or result}")
        return self._token_request({
            "grant_type": "authorization_code", "code": result["code"],
            "redirect_uri": self.redirect_uri, "code_verifier": verifier,
        })

    # -- tokens --------------------------------------------------------------
    def _token_request(self, params: dict) -> dict:
        status, body = self._http("POST", TOKEN_URL, data={
            "client_key": self.client_key, "client_secret": self.client_secret, **params})
        if status != 200 or "access_token" not in body:
            raise TikTokAuthError(f"TikTok token request failed ({status}): "
                                  f"{body.get('error_description') or body.get('error') or body}")
        now = time.time()
        token = {
            "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token"),
            "open_id": body.get("open_id"),
            "scope": body.get("scope"),
            "expires_at": now + int(body.get("expires_in", 0)),
            "refresh_expires_at": now + int(body.get("refresh_expires_in", 0)),
        }
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps(token, indent=2), encoding="utf-8")
        try:
            self.token_file.chmod(0o600)
        except OSError:
            pass
        return token

    def access_token(self) -> str:
        if not self.token_file.exists():
            raise TikTokAuthError("not logged in to TikTok: run `python -m shortpipe tiktok-auth`")
        token = json.loads(self.token_file.read_text(encoding="utf-8"))
        if time.time() < token["expires_at"] - 60:
            return token["access_token"]
        if not token.get("refresh_token") or time.time() > token.get("refresh_expires_at", 0):
            raise TikTokAuthError("TikTok login expired: run `python -m shortpipe tiktok-auth`")
        log.info("Refreshing TikTok access token")
        return self._token_request({"grant_type": "refresh_token",
                                    "refresh_token": token["refresh_token"]})["access_token"]


# ---------------------------------------------------------------------------
# Source


class TikTokDisplaySource(VideoSource):
    name = "tiktok"

    def __init__(self, auth: TikTokAuth, video_dir: Path, max_videos: int = 50,
                 http: HttpFn = _http, max_retries: int = 4):
        self.auth = auth
        self.video_dir = Path(video_dir)
        self.max_videos = max_videos
        self._http = http
        self.max_retries = max_retries

    def _api(self, method: str, url: str, **kwargs) -> dict:
        for attempt in range(1, self.max_retries + 1):
            headers = {"Authorization": f"Bearer {self.auth.access_token()}"}
            status, body = self._http(method, url, headers=headers, **kwargs)
            error = body.get("error") or {}
            code = error.get("code", "ok") if isinstance(error, dict) else str(error)
            if status == 200 and code == "ok":
                return body.get("data") or {}
            if code in {"access_token_invalid", "scope_not_authorized", "scope_permission_missed"} \
                    or status == 401:
                raise TikTokAuthError(f"TikTok rejected the login ({code}): run "
                                      "`python -m shortpipe tiktok-auth`")
            if code == "rate_limit_exceeded" or status == 429 or status >= 500:
                delay = 2 ** attempt
                log.warning("TikTok API %s (HTTP %d); retry %d/%d in %ds",
                            code, status, attempt, self.max_retries, delay)
                time.sleep(delay)
                continue
            raise TikTokError(f"TikTok API error ({status} {code}): {error.get('message', '')}")
        raise TikTokError(f"TikTok API still failing after {self.max_retries} retries")

    def _display_name(self) -> str:
        data = self._api("GET", USER_INFO_URL + "?fields=open_id,display_name")
        user = data.get("user", {})
        return user.get("display_name") or user.get("open_id") or "TikTok account owner"

    def _local_file(self, video_id: str) -> Path | None:
        for ext in VIDEO_EXTENSIONS:
            candidate = self.video_dir / f"{video_id}{ext}"
            if candidate.is_file():
                return candidate
        # Also accept files that merely contain the id, e.g. "my clip 7351234567890.mp4"
        if self.video_dir.is_dir():
            for candidate in self.video_dir.iterdir():
                if video_id in candidate.stem and candidate.suffix.lower() in VIDEO_EXTENSIONS:
                    return candidate
        return None

    def discover(self) -> Iterator[SourceItem]:
        owner = self._display_name()
        cursor, fetched = None, 0
        while fetched < self.max_videos:
            body = {"max_count": min(20, self.max_videos - fetched)}
            if cursor is not None:
                body["cursor"] = cursor
            data = self._api("POST", f"{VIDEO_LIST_URL}?fields={VIDEO_FIELDS}", json_body=body)
            videos = data.get("videos", [])
            for v in videos:
                fetched += 1
                video_id = str(v["id"])
                text = v.get("video_description") or ""
                local = self._local_file(video_id)
                if local is None:
                    log.info("TikTok video %s has no local file yet (expected %s/%s.mp4)",
                             video_id, self.video_dir, video_id)
                yield SourceItem(
                    source=self.name,
                    source_id=video_id,
                    title=v.get("title") or text[:90],
                    description=text,
                    creator=owner,
                    license="owned",           # Display API returns only your own videos
                    rights_holder=owner,
                    file_path=str(local) if local else None,
                    duration_seconds=v.get("duration"),
                    views=int(v.get("view_count") or 0),
                    likes=int(v.get("like_count") or 0),
                    comments=int(v.get("comment_count") or 0),
                    shares=int(v.get("share_count") or 0),
                    hashtags=re.findall(r"#(\w+)", text),
                )
            if not data.get("has_more") or not videos:
                break
            cursor = data.get("cursor")
        log.info("TikTok: listed %d of your videos (account: %s)", fetched, owner)
