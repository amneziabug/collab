from datetime import date, datetime, timezone

from shortpipe.ai.metadata import MAX_TAGS_CHARS, sanitize
from shortpipe.ranking import engagement_score, final_score
from shortpipe.scheduler import daily_slots, free_slots
from shortpipe.sources.rights import check_rights
from shortpipe.youtube.uploader import build_body

ALLOWED = ["owned", "cc0", "cc-by", "written-permission"]


def test_rights_check():
    assert check_rights({"license": "owned", "rights_holder": "me"}, ALLOWED)[0]
    assert not check_rights({"license": "", "rights_holder": "me"}, ALLOWED)[0]
    assert not check_rights({"license": "all-rights-reserved", "rights_holder": "x"}, ALLOWED)[0]
    assert not check_rights({"license": "owned", "rights_holder": ""}, ALLOWED)[0]
    assert not check_rights({"license": "cc-by", "rights_holder": "x"}, ALLOWED)[0]
    assert check_rights({"license": "cc-by", "rights_holder": "x", "attribution": "X, CC BY"}, ALLOWED)[0]
    assert not check_rights({"license": "owned", "rights_holder": "me",
                             "file_path": "https://example.com/a.mp4"}, ALLOWED)[0]


def test_scores_are_bounded():
    assert engagement_score(0, 0, 0, 0) == 0
    assert engagement_score(100, 100, 100, 100) == 1.0
    _, score = final_score({"views": 10_000, "likes": 800, "comments": 50, "shares": 20}, 0.9, 0.8)
    assert 0 < score <= 1


def test_daily_slots_spread_inside_window():
    slots = daily_slots(date(2026, 1, 1), 5, "09:00", "21:00", "UTC")
    assert len(slots) == 5
    assert slots[0].hour >= 9 and slots[-1].hour < 21
    gaps = {(b - a).total_seconds() for a, b in zip(slots, slots[1:])}
    assert len(gaps) == 1


def test_free_slots_skip_past_and_taken():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    slots = free_slots(now, set(), 4, "09:00", "21:00", "UTC", 0)
    assert all(s > "2026-01-01T12:00" for s in slots)
    taken = {slots[0]}
    assert slots[0] not in free_slots(now, taken, 4, "09:00", "21:00", "UTC", 0)


def test_metadata_sanitized_to_youtube_limits():
    meta = sanitize({"title": "<b>" + "x" * 200, "description": "d<>", "tags": ["#a" * 3] + [f"tag{i}" for i in range(300)]},
                    attribution="Jane, CC BY")
    assert len(meta["title"]) <= 100 and "<" not in meta["title"]
    assert "Credit: Jane, CC BY" in meta["description"]
    assert sum(len(t) for t in meta["tags"]) + len(meta["tags"]) - 1 <= MAX_TAGS_CHARS


def test_upload_body_privacy():
    body = build_body("t", "d", ["a"], "27", "en")
    assert body["status"]["privacyStatus"] == "private"     # default
    assert "publishAt" not in body["status"]
    assert build_body("t", "d", [], "27", "en", "public")["status"]["privacyStatus"] == "public"
    import pytest
    with pytest.raises(ValueError):
        build_body("t", "d", [], "27", "en", "everyone")


def test_upload_body_with_publish_time():
    body = build_body("t", "d", [], "27", "en", "public", publish_at="2026-10-08T18:00:00Z")
    assert body["status"]["privacyStatus"] == "private"
    assert body["status"]["publishAt"] == "2026-10-08T18:00:00Z"
