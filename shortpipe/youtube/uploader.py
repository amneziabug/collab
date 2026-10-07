"""Resumable uploads via the YouTube Data API v3. Uploads are private unless
`youtube.privacy` says otherwise.

Note: Google locks videos uploaded through the API by *unaudited* projects to private,
whatever privacyStatus is requested; the uploader detects and reports that."""

from __future__ import annotations

import http.client
import logging
import random
import time
from pathlib import Path

log = logging.getLogger(__name__)

PRIVACY_OPTIONS = ("private", "unlisted", "public")
LOCKED_PRIVATE_HINT = (
    "YouTube kept it private: uploads through the API from unaudited Google Cloud projects are "
    "locked to private. To publish via the API, request the YouTube API compliance audit for your "
    "project (https://support.google.com/youtube/contact/yt_api_form).")

RETRIABLE_STATUS = {500, 502, 503, 504}
RETRIABLE_EXCEPTIONS = (OSError, http.client.HTTPException)
CHUNK_SIZE = 8 * 1024 * 1024


class UploadError(RuntimeError):
    pass


class YouTubeAuthError(UploadError):
    """Login missing, expired or revoked; run `python -m shortpipe auth`."""


class QuotaExceededError(UploadError):
    """Daily API quota used up; stop uploading until it resets (midnight Pacific)."""


def build_body(title: str, description: str, tags: list[str], category_id: str,
               language: str, privacy: str = "private") -> dict:
    if privacy not in PRIVACY_OPTIONS:
        raise ValueError(f"privacy must be one of {PRIVACY_OPTIONS}")
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category_id,
            "defaultLanguage": language,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
            "embeddable": privacy == "public",
        },
    }
    return body


class YouTubeUploader:
    def __init__(self, credentials, max_retries: int = 5):
        from googleapiclient.discovery import build

        self.service = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        self.max_retries = max_retries
        self.last_privacy: str | None = None  # privacy YouTube actually applied to the last upload

    def upload(self, file_path: Path, *, title: str, description: str, tags: list[str],
               category_id: str, language: str, privacy: str = "private") -> str:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        file_path = Path(file_path)
        if not file_path.is_file():
            raise UploadError(f"file not found: {file_path}")

        body = build_body(title, description, tags, category_id, language, privacy)
        media = MediaFileUpload(str(file_path), mimetype="video/mp4", chunksize=CHUNK_SIZE, resumable=True)
        request = self.service.videos().insert(part="snippet,status", body=body,
                                               media_body=media, notifySubscribers=False)

        response, retry = None, 0
        while response is None:
            try:
                status, response = request.next_chunk()
                if status:
                    log.debug("Upload %s: %d%%", file_path.name, int(status.progress() * 100))
            except HttpError as exc:
                reason = _error_reason(exc)
                if exc.resp.status == 401:
                    raise YouTubeAuthError(f"YouTube rejected the login ({reason or 'unauthorized'}); "
                                           "run `python -m shortpipe auth`") from exc
                if exc.resp.status == 403 and reason in {"quotaExceeded", "uploadLimitExceeded"}:
                    raise QuotaExceededError(reason) from exc
                if exc.resp.status not in RETRIABLE_STATUS:
                    raise UploadError(f"HTTP {exc.resp.status} {reason}: {exc}") from exc
                retry = self._backoff(retry, exc)
            except RETRIABLE_EXCEPTIONS as exc:
                retry = self._backoff(retry, exc)

        video_id = response.get("id")
        if not video_id:
            raise UploadError(f"unexpected response: {response}")
        applied = response.get("status", {}).get("privacyStatus") or privacy
        self.last_privacy = applied
        if applied != privacy:
            log.warning("Video %s: asked for %r but it is %r. %s", video_id, privacy, applied,
                        LOCKED_PRIVATE_HINT if applied == "private" else "")
        return video_id

    def _backoff(self, retry: int, exc: Exception) -> int:
        retry += 1
        if retry > self.max_retries:
            raise UploadError(f"giving up after {self.max_retries} retries: {exc}") from exc
        delay = random.uniform(0, 2 ** retry)
        log.warning("Retriable upload error (%s); retry %d in %.1fs", exc, retry, delay)
        time.sleep(delay)
        return retry


def _error_reason(exc) -> str:
    try:
        import json
        data = json.loads(exc.content.decode())
        return data["error"]["errors"][0]["reason"]
    except Exception:
        return ""
