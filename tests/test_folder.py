"""Folder source + optional vision step."""

import json
import subprocess

import pytest

from shortpipe.ai.client import OfflineClient
from shortpipe.pipeline import Pipeline
from shortpipe.sources.folder import FolderSource, title_from_filename

from .conftest import needs_ffmpeg
from .test_ollama_client import FakeOllama


def make_clip(path, seconds=2, title=None):
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=30:duration={seconds}",
           "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if title:
        cmd += ["-metadata", f"title={title}", "-metadata", "comment=Filmed for my physics class #physics"]
    subprocess.run(cmd + [str(path)], check=True)


def test_title_from_filename(tmp_path):
    assert title_from_filename(tmp_path / "my_physics-demo.final.mp4") == "My physics demo final"


@needs_ffmpeg
def test_folder_discovery_reads_files_and_tags(tmp_path):
    folder = tmp_path / "vids"
    folder.mkdir()
    make_clip(folder / "pendulum_experiment.mp4")
    make_clip(folder / "IMG_0042.mov", title="Pendulum in slow motion")
    (folder / "notes.txt").write_text("not a video")
    items = {i.file_path.split("/")[-1]: i for i in FolderSource(folder, "Tamako").discover()}
    assert set(items) == {"pendulum_experiment.mp4", "IMG_0042.mov"}
    plain, tagged = items["pendulum_experiment.mp4"], items["IMG_0042.mov"]
    assert plain.title == "Pendulum experiment" and plain.license == "owned"
    assert plain.rights_holder == "Tamako" and plain.has_stats is False
    assert round(plain.duration_seconds) == 2
    assert tagged.title == "Pendulum in slow motion" and tagged.hashtags == ["physics"]


def test_missing_folder_is_reported(tmp_path):
    with pytest.raises(FileNotFoundError, match="folder"):
        list(FolderSource(tmp_path / "nope", "me").discover())


@needs_ffmpeg
def test_folder_videos_skip_view_threshold_and_are_auto_approved(cfg, db, uploader, tmp_path):
    folder = tmp_path / "vids"
    folder.mkdir()
    make_clip(folder / "cooking_pasta.mp4")          # off-topic for the configured topics
    pipe = Pipeline(cfg, db, OfflineClient, lambda: uploader)
    pipe.discover(FolderSource(folder, "Tamako"))
    assert pipe.analyze_and_rank() == 1
    row = db.conn.execute("SELECT * FROM videos").fetchone()
    assert row["status"] == "selected" and "auto-approved" in row["status_reason"]
    assert pipe.process() == 1 and pipe.generate_metadata() == 1 and pipe.schedule() == 1


@needs_ffmpeg
def test_empty_owner_is_rejected(cfg, db, uploader, tmp_path):
    folder = tmp_path / "vids"
    folder.mkdir()
    make_clip(folder / "a.mp4")
    Pipeline(cfg, db, OfflineClient, lambda: uploader).discover(FolderSource(folder, ""))
    row = db.conn.execute("SELECT * FROM videos").fetchone()
    assert row["status"] == "rejected" and "rights holder" in row["status_reason"]


@needs_ffmpeg
def test_vision_summary_feeds_metadata(cfg, db, uploader, tmp_path):
    from shortpipe.ai.client import OllamaClient

    fake = FakeOllama()
    try:
        fake.replies.append((200, {"message": {"role": "assistant",
                                               "content": "A pendulum swings in front of a ruler."}}))
        folder = tmp_path / "vids"
        folder.mkdir()
        make_clip(folder / "IMG_0001.mp4")
        cfg.ai.vision_frames = 2
        pipe = Pipeline(cfg, db, OfflineClient, lambda: uploader,
                        vision_factory=lambda: OllamaClient("gemma3:4b", fake.url))
        pipe.discover(FolderSource(folder, "Tamako"))
        pipe.analyze_and_rank()
        row = db.conn.execute("SELECT * FROM videos").fetchone()
        assert row["visual_summary"] == "A pendulum swings in front of a ruler."
        _, body = fake.requests[0]
        assert body["model"] == "gemma3:4b" and len(body["messages"][0]["images"]) == 2
        stages = [r[0] for r in db.conn.execute("SELECT stage FROM ai_decisions ORDER BY id")]
        assert stages == ["vision", "analysis"]
        prompt = db.conn.execute("SELECT prompt FROM ai_decisions WHERE stage='analysis'").fetchone()[0]
        assert json.loads(prompt)["video"]["visual_summary"].startswith("A pendulum")
    finally:
        fake.server.shutdown()


@needs_ffmpeg
def test_vision_failure_is_not_fatal(cfg, db, uploader, tmp_path):
    from shortpipe.ai.client import OllamaClient

    fake = FakeOllama()
    try:
        fake.replies.append((500, {"error": "model crashed"}))
        folder = tmp_path / "vids"
        folder.mkdir()
        make_clip(folder / "clip.mp4")
        pipe = Pipeline(cfg, db, OfflineClient, lambda: uploader,
                        vision_factory=lambda: OllamaClient("gemma3:4b", fake.url))
        pipe.discover(FolderSource(folder, "Tamako"))
        assert pipe.analyze_and_rank() == 1
        assert db.conn.execute("SELECT visual_summary FROM videos").fetchone()[0] is None
    finally:
        fake.server.shutdown()
