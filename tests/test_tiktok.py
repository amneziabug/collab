"""TikTok Display API source with a fake HTTP layer (no network)."""

import hashlib
import json
import time

import pytest

from shortpipe.sources.tiktok import (TOKEN_URL, USER_INFO_URL, VIDEO_LIST_URL, TikTokAuth,
                                      TikTokAuthError, TikTokDisplaySource, TikTokError, pkce_pair)


class FakeHttp:
    def __init__(self):
        self.calls = []
        self.routes = {}   # url prefix -> list of (status, body)

    def add(self, prefix, status, body):
        self.routes.setdefault(prefix, []).append((status, body))

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for prefix, replies in self.routes.items():
            if url.startswith(prefix):
                return replies.pop(0) if len(replies) > 1 else replies[0]
        raise AssertionError(f"unexpected request {url}")


def ok(data):
    return {"data": data, "error": {"code": "ok", "message": "", "log_id": "x"}}


def make_auth(tmp_path, http, expires_in=3600):
    token_file = tmp_path / "tiktok_token.json"
    token_file.write_text(json.dumps({
        "access_token": "act.1", "refresh_token": "rft.1", "open_id": "u1",
        "expires_at": time.time() + expires_in, "refresh_expires_at": time.time() + 86400,
    }))
    return TikTokAuth("key", "secret", token_file, http=http)


def video(i, **extra):
    return {"id": str(7000 + i), "title": f"clip {i}", "video_description": f"Learn math #math #study{i}",
            "duration": 30, "view_count": 1000 * i, "like_count": 100 * i,
            "comment_count": i, "share_count": i, **extra}


def test_pkce_challenge_is_hex_sha256():
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert challenge == hashlib.sha256(verifier.encode()).hexdigest()


def test_lists_own_videos_with_pagination_and_local_files(tmp_path):
    http = FakeHttp()
    http.add(USER_INFO_URL, 200, ok({"user": {"display_name": "Tamako"}}))
    http.add(VIDEO_LIST_URL, 200, ok({"videos": [video(1), video(2)], "cursor": 99, "has_more": True}))
    http.add(VIDEO_LIST_URL, 200, ok({"videos": [video(3)], "cursor": 100, "has_more": False}))
    vids = tmp_path / "tiktok"
    vids.mkdir()
    (vids / "7001.mp4").write_bytes(b"x")
    (vids / "my export 7003.mov").write_bytes(b"y")

    items = list(TikTokDisplaySource(make_auth(tmp_path, http), vids, http=http).discover())

    assert [i.source_id for i in items] == ["7001", "7002", "7003"]
    assert all(i.license == "owned" and i.rights_holder == "Tamako" for i in items)
    assert items[0].file_path.endswith("7001.mp4") and items[1].file_path is None
    assert items[2].file_path.endswith("my export 7003.mov")
    assert items[0].hashtags == ["math", "study1"] and items[2].views == 3000
    list_calls = [c for c in http.calls if c[1].startswith(VIDEO_LIST_URL)]
    assert "cursor" not in list_calls[0][2]["json_body"] and list_calls[1][2]["json_body"]["cursor"] == 99
    assert list_calls[0][2]["headers"]["Authorization"] == "Bearer act.1"


def test_expired_token_is_refreshed(tmp_path):
    http = FakeHttp()
    http.add(TOKEN_URL, 200, {"access_token": "act.2", "refresh_token": "rft.2", "expires_in": 86400,
                              "refresh_expires_in": 31536000, "open_id": "u1"})
    auth = make_auth(tmp_path, http, expires_in=-10)
    assert auth.access_token() == "act.2"
    assert http.calls[0][2]["data"]["grant_type"] == "refresh_token"
    assert json.loads(auth.token_file.read_text())["access_token"] == "act.2"


def test_missing_login_and_rejected_token_are_auth_errors(tmp_path):
    auth = TikTokAuth("key", "secret", tmp_path / "none.json", http=FakeHttp())
    with pytest.raises(TikTokAuthError, match="tiktok-auth"):
        auth.access_token()
    http = FakeHttp()
    http.add(USER_INFO_URL, 401, {"error": {"code": "access_token_invalid", "message": "bad"}})
    with pytest.raises(TikTokAuthError):
        list(TikTokDisplaySource(make_auth(tmp_path, http), tmp_path, http=http).discover())


def test_rate_limit_is_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    http = FakeHttp()
    http.add(USER_INFO_URL, 429, {"error": {"code": "rate_limit_exceeded", "message": "slow down"}})
    http.add(USER_INFO_URL, 200, ok({"user": {"display_name": "Tamako"}}))
    http.add(VIDEO_LIST_URL, 200, ok({"videos": [], "has_more": False}))
    assert list(TikTokDisplaySource(make_auth(tmp_path, http), tmp_path, http=http).discover()) == []
    assert sum(1 for c in http.calls if c[1].startswith(USER_INFO_URL)) == 2


def test_persistent_api_error_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    http = FakeHttp()
    http.add(USER_INFO_URL, 500, {"error": {"code": "internal_error", "message": "boom"}})
    with pytest.raises(TikTokError, match="retries"):
        list(TikTokDisplaySource(make_auth(tmp_path, http), tmp_path, http=http, max_retries=2).discover())


def test_missing_client_credentials(tmp_path):
    with pytest.raises(TikTokAuthError, match="TIKTOK_CLIENT_KEY"):
        TikTokAuth("", "", tmp_path / "t.json")
