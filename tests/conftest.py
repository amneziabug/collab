import shutil
from pathlib import Path

import pytest

from shortpipe.ai.client import OfflineClient
from shortpipe.config import Config
from shortpipe.db import Database
from shortpipe.pipeline import Pipeline


class FakeUploader:
    def __init__(self, fail_times: int = 0):
        self.calls = []
        self.fail_times = fail_times

    def upload(self, path, **kwargs):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("simulated network failure")
        self.calls.append((Path(path), kwargs))
        return f"vid{len(self.calls)}"


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.paths.database = tmp_path / "test.db"
    c.paths.dataset_manifest = tmp_path / "dataset" / "manifest.json"
    c.paths.processed_dir = tmp_path / "processed"
    c.paths.log_dir = tmp_path / "logs"
    c.ai.provider = "offline"
    c.selection.topics = ["science", "programming", "study tips", "math"]
    c.schedule.timezone = "UTC"
    return c


@pytest.fixture
def db(cfg):
    d = Database(cfg.paths.database)
    yield d
    d.close()


@pytest.fixture
def uploader():
    return FakeUploader()


@pytest.fixture
def pipeline(cfg, db, uploader):
    return Pipeline(cfg, db, ai_factory=OfflineClient, uploader_factory=lambda: uploader)


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
