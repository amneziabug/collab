import json
from datetime import datetime, timedelta, timezone

from shortpipe.demo_dataset import make_demo_dataset
from shortpipe.sources import LocalDatasetSource

from .conftest import FakeUploader, needs_ffmpeg


def _statuses(db):
    return {r["source_id"]: r["status"] for r in db.conn.execute("SELECT source_id, status FROM videos")}


@needs_ffmpeg
def test_end_to_end(cfg, db, pipeline, uploader):
    manifest = make_demo_dataset(cfg.paths.dataset_manifest.parent, seconds=2)
    source = LocalDatasetSource(manifest)

    result = pipeline.run_all(source)
    assert result["discovered"] == 7
    st = _statuses(db)
    assert st["demo-007"] == "rejected"   # no license
    assert st["demo-005"] == "rejected"   # off-topic
    assert st["demo-006"] == "rejected"   # too few views
    assert result["scheduled"] == 4

    # Re-running is idempotent.
    assert pipeline.discover(source) == 0

    # Jump past all slots and upload.
    later = datetime.now(timezone.utc) + timedelta(days=5)
    assert pipeline.upload_due(now=later) == 4
    assert len(uploader.calls) == 4
    for path, kwargs in uploader.calls:
        assert path.exists() and kwargs["title"]
    row = db.conn.execute("SELECT * FROM videos WHERE source_id='demo-004'").fetchone()
    assert "Credit: Demo Student, CC BY 4.0" in row["yt_description"]
    assert db.conn.execute("SELECT COUNT(*) FROM ai_decisions").fetchone()[0] >= 8


def test_upload_retries_then_fails(cfg, db):
    from shortpipe.ai.client import OfflineClient
    from shortpipe.pipeline import MAX_ATTEMPTS, Pipeline

    vid, _ = db.upsert_discovered({"source": "t", "source_id": "1", "title": "x", "views": 1})
    db.update(vid, status="scheduled", scheduled_at="2020-01-01T00:00:00+00:00",
              processed_path="/nonexistent.mp4", yt_title="t", yt_description="d", yt_tags=[])
    flaky = FakeUploader(fail_times=99)
    pipe = Pipeline(cfg, db, OfflineClient, lambda: flaky)
    for _ in range(MAX_ATTEMPTS):
        pipe.upload_due()
    row = db.get(vid)
    assert row["status"] == "failed" and row["attempts"] == MAX_ATTEMPTS
    assert "simulated" in row["last_error"]


def test_schedule_respects_daily_limit(cfg, db, pipeline):
    for i in range(12):
        vid, _ = db.upsert_discovered({"source": "t", "source_id": str(i), "title": f"v{i}"})
        db.update(vid, status="metadata_ready", yt_title=f"v{i}")
    cfg.schedule.uploads_per_day = 4
    cfg.schedule.days_ahead = 1
    now = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    assert pipeline.schedule(now=now) == 8
    days = [r["scheduled_at"][:10] for r in db.by_status("scheduled")]
    assert days.count("2026-01-01") == 4 and days.count("2026-01-02") == 4


def test_missing_api_key_does_not_fail_videos(cfg, db, uploader):
    from shortpipe.ai import AIConfigError
    from shortpipe.pipeline import Pipeline

    def no_key():
        raise AIConfigError("No Claude credentials found")

    vid, _ = db.upsert_discovered({"source": "t", "source_id": "1", "title": "science",
                                   "views": 5000, "license": "owned", "rights_holder": "me"})
    pipe = Pipeline(cfg, db, no_key, lambda: uploader)
    import pytest
    with pytest.raises(AIConfigError):
        pipe.analyze_and_rank()
    assert db.get(vid)["status"] == "discovered"


def test_retry_failed_resets_to_failed_stage(db, pipeline):
    ids = {}
    for stage in ("analysis", "processing", "metadata", "upload"):
        vid, _ = db.upsert_discovered({"source": "t", "source_id": stage, "title": stage})
        db.set_status(vid, "failed", f"{stage}: boom", attempts=3, last_error="boom")
        ids[stage] = vid
    assert pipeline.retry_failed() == 4
    assert db.get(ids["analysis"])["status"] == "discovered"
    assert db.get(ids["processing"])["status"] == "selected"
    assert db.get(ids["metadata"])["status"] == "processed"
    up = db.get(ids["upload"])
    assert up["status"] == "metadata_ready" and up["attempts"] == 0 and up["last_error"] is None


def test_selection_uses_score_not_model_recommend(cfg, db, uploader):
    """A local model may answer relevance=1.0 but recommend=False; the score decides."""
    from shortpipe.ai.client import AIClient
    from shortpipe.pipeline import Pipeline

    class Contradictory(AIClient):
        model = "contradictory"

        def complete_json(self, task, system, user, schema):
            return {"topic": "science", "relevance": 1.0, "educational_value": 0.9,
                    "content_flags": [], "recommend": False, "reasoning": "great but no"}

    vid, _ = db.upsert_discovered({"source": "t", "source_id": "1", "title": "sky",
                                   "views": 5000, "likes": 500})
    Pipeline(cfg, db, Contradictory, lambda: uploader).analyze_and_rank()
    assert db.get(vid)["status"] == "selected"


class _ListSource:
    name = "t"

    def __init__(self, items):
        self.items = items

    def discover(self):
        from shortpipe.sources import SourceItem
        return [SourceItem(**i) for i in self.items]


@needs_ffmpeg
def test_duplicate_files_are_rejected(cfg, db, pipeline, tmp_path):
    import shutil
    manifest = make_demo_dataset(cfg.paths.dataset_manifest.parent, seconds=1)
    original = manifest.parent / "demo-001.mp4"
    copy = tmp_path / "renamed-copy.mp4"
    shutil.copy(original, copy)
    base = {"source": "t", "title": "Why the sky is blue: science", "license": "owned",
            "rights_holder": "me", "views": 50000, "likes": 5000}
    pipeline.discover(_ListSource([{**base, "source_id": "a", "file_path": str(original)},
                                   {**base, "source_id": "b", "file_path": str(copy)}]))
    pipeline.analyze_and_rank()
    pipeline.process()
    st = _statuses(db)
    assert sorted(st.values()) == ["processed", "rejected"]
    rejected = db.conn.execute("SELECT status_reason FROM videos WHERE status='rejected'").fetchone()
    assert rejected[0].startswith("duplicate of #")


def test_missing_file_waits_and_is_picked_up_later(cfg, db, pipeline, tmp_path):
    item = {"source": "t", "source_id": "x", "title": "math science", "license": "owned",
            "rights_holder": "me", "views": 50000, "likes": 5000}
    pipeline.discover(_ListSource([item]))
    pipeline.analyze_and_rank()
    assert pipeline.process() == 0
    row = db.conn.execute("SELECT * FROM videos WHERE source_id='x'").fetchone()
    assert row["status"] == "selected" and row["status_reason"] == "waiting for local file"
    late = tmp_path / "x.mp4"
    late.write_bytes(b"later")
    pipeline.discover(_ListSource([{**item, "file_path": str(late)}]))
    assert db.get(row["id"])["file_path"] == str(late)


def test_youtube_auth_error_does_not_use_up_attempts(cfg, db):
    from shortpipe.ai.client import OfflineClient
    from shortpipe.pipeline import Pipeline
    from shortpipe.youtube.uploader import YouTubeAuthError

    class Expired:
        def upload(self, *a, **k):
            raise YouTubeAuthError("expired")

    ids = []
    for i in range(2):
        vid, _ = db.upsert_discovered({"source": "t", "source_id": str(i), "title": "x"})
        db.update(vid, status="scheduled", scheduled_at="2020-01-01T00:00:00+00:00",
                  processed_path="/x.mp4", yt_title="t", yt_description="d", yt_tags=[])
        ids.append(vid)
    Pipeline(cfg, db, OfflineClient, lambda: Expired()).upload_due()
    for vid in ids:
        row = db.get(vid)
        assert row["status"] == "scheduled" and row["attempts"] == 0


@needs_ffmpeg
def test_upload_now_uploads_everything_immediately(cfg, db, pipeline, uploader):
    manifest = make_demo_dataset(cfg.paths.dataset_manifest.parent, seconds=1)
    source = LocalDatasetSource(manifest)
    preview = pipeline.upload_now(source, dry_run=True)
    assert preview["queued"] == 4 and uploader.calls == []
    assert _statuses(db)["demo-001"] == "metadata_ready"      # dry run changed nothing
    result = pipeline.upload_now(source)
    assert result["uploaded"] == 4 and result["still_queued"] == 0
    assert len(uploader.calls) == 4
    assert list(_statuses(db).values()).count("uploaded") == 4


def test_upload_now_keeps_rest_queued_on_quota(cfg, db):
    from shortpipe.ai.client import OfflineClient
    from shortpipe.pipeline import Pipeline
    from shortpipe.youtube.uploader import QuotaExceededError

    class QuotaAfterOne:
        n = 0

        def upload(self, *a, **k):
            self.n += 1
            if self.n > 1:
                raise QuotaExceededError("quotaExceeded")
            return "vid1"

    for i in range(3):
        vid, _ = db.upsert_discovered({"source": "t", "source_id": str(i), "title": "x"})
        db.update(vid, status="metadata_ready", processed_path="/x.mp4", yt_title=f"t{i}",
                  yt_description="d", yt_tags=[])
    result = Pipeline(cfg, db, OfflineClient, lambda: QuotaAfterOne()).upload_now([])
    assert result["uploaded"] == 1 and result["still_queued"] == 2
    assert all(r["attempts"] == 0 for r in db.by_status("scheduled"))


def test_title_and_description_overrides(cfg, db, uploader):
    from shortpipe.ai.client import OfflineClient
    from shortpipe.pipeline import Pipeline

    cfg.youtube.title = "#fyp #viral"
    cfg.youtube.description = "none"
    vid, _ = db.upsert_discovered({"source": "folder", "source_id": "a", "title": "x"})
    db.update(vid, status="scheduled", scheduled_at="2020-01-01T00:00:00+00:00", processed_path="/x.mp4",
              yt_title="AI title", yt_description="AI description", yt_tags=["a"])
    Pipeline(cfg, db, OfflineClient, lambda: uploader).upload_due()
    _, kwargs = uploader.calls[0]
    assert kwargs["title"] == "#fyp #viral" and kwargs["description"] == ""
    row = db.get(vid)
    assert row["yt_title"] == "#fyp #viral" and row["yt_description"] == ""


def test_cc_by_credit_kept_without_description(cfg, db, uploader):
    from shortpipe.ai.client import OfflineClient
    from shortpipe.pipeline import Pipeline

    cfg.youtube.description = "none"
    vid, _ = db.upsert_discovered({"source": "t", "source_id": "a", "title": "x", "attribution": "Jane, CC BY"})
    db.update(vid, status="scheduled", scheduled_at="2020-01-01T00:00:00+00:00", processed_path="/x.mp4",
              yt_title="T", yt_description="D", yt_tags=[])
    Pipeline(cfg, db, OfflineClient, lambda: uploader).upload_due()
    assert uploader.calls[0][1]["description"] == "Credit: Jane, CC BY"


def test_own_folder_ignores_minor_flags_but_not_serious_ones(cfg, db, uploader):
    from shortpipe.ai.client import AIClient
    from shortpipe.pipeline import Pipeline

    class Flagging(AIClient):
        model = "flagging"
        flags = {}

        def complete_json(self, task, system, user, schema):
            title = json.loads(user)["video"]["title"]
            return {"topic": "other", "relevance": 0.0, "educational_value": 0.0,
                    "content_flags": self.flags[title], "recommend": False, "reasoning": "-"}

    Flagging.flags = {"minor": ["not_educational", "low_quality"], "bad": ["unsafe"]}
    for t in Flagging.flags:
        db.upsert_discovered({"source": "folder", "source_id": t, "title": t, "has_stats": False,
                              "license": "owned", "rights_holder": "me"})
    Pipeline(cfg, db, Flagging, lambda: uploader).analyze_and_rank()
    st = _statuses(db)
    assert st["minor"] == "selected" and st["bad"] == "rejected"


def test_privacy_setting_is_passed_and_actual_privacy_recorded(cfg, db):
    from shortpipe.ai.client import OfflineClient
    from shortpipe.pipeline import Pipeline

    class LockedToPrivate:
        last_privacy = None

        def upload(self, *a, privacy, **k):
            self.requested = privacy
            self.last_privacy = "private"     # what YouTube does for unaudited projects
            return "vid1"

    cfg.youtube.privacy = "public"
    up = LockedToPrivate()
    vid, _ = db.upsert_discovered({"source": "t", "source_id": "a", "title": "x"})
    db.update(vid, status="scheduled", scheduled_at="2020-01-01T00:00:00+00:00", processed_path="/x.mp4",
              yt_title="T", yt_description="D", yt_tags=[])
    Pipeline(cfg, db, OfflineClient, lambda: up).upload_due()
    assert up.requested == "public"
    assert db.get(vid)["status_reason"] == "private"
