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
