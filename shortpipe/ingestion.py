"""Video ingestion: find the file for a selected video from a configurable chain of providers.

The rest of the pipeline only calls `Ingestor.locate(video)`, so where files come from can be
changed in config (or by adding a provider class) without touching discovery, ranking,
processing or upload.

Ingestion runs only for videos that passed the rights check at discovery (`check_rights`), and
the pipeline re-checks rights immediately before ingesting. Trend data (popular videos used for
analysis, see `trends.py`) is stored separately and never reaches this stage.

Built-in providers (`[ingestion] providers` in config.toml, tried in order):
  source_file  - the path the source itself reported (e.g. the local manifest's "file")
  folder       - a file in one of `[ingestion] folders` whose name contains the video's
                 source id, e.g. data/tiktok/7351234567890.mp4 for your own TikTok export
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Mapping, Sequence

log = logging.getLogger(__name__)

VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v", ".webm", ".mkv")


class Ingestor(ABC):
    name: str

    @abstractmethod
    def locate(self, video: Mapping) -> Path | None:
        """Return a readable local file for `video`, or None if this provider has none."""


class SourceFileIngestor(Ingestor):
    name = "source_file"

    def locate(self, video: Mapping) -> Path | None:
        path = video["file_path"]
        if path and Path(path).is_file():
            return Path(path)
        return None


class FolderIngestor(Ingestor):
    name = "folder"

    def __init__(self, folders: Sequence[Path | str]):
        self.folders = [Path(f) for f in folders]

    def locate(self, video: Mapping) -> Path | None:
        source_id = str(video["source_id"])
        for folder in self.folders:
            if not folder.is_dir():
                continue
            exact = [folder / f"{source_id}{ext}" for ext in VIDEO_EXTENSIONS]
            for candidate in exact:
                if candidate.is_file():
                    return candidate
            for candidate in sorted(folder.iterdir()):
                if source_id in candidate.stem and candidate.suffix.lower() in VIDEO_EXTENSIONS:
                    return candidate
        return None


class ChainIngestor(Ingestor):
    name = "chain"

    def __init__(self, providers: Sequence[Ingestor]):
        self.providers = list(providers)

    def locate(self, video: Mapping) -> Path | None:
        for provider in self.providers:
            path = provider.locate(video)
            if path is not None:
                log.debug("Ingestion: #%s found by %s at %s", video["id"], provider.name, path)
                return path
        return None

    def describe(self) -> str:
        return " -> ".join(p.name for p in self.providers)


PROVIDERS = {"source_file", "folder"}


def build_ingestor(cfg) -> ChainIngestor:
    providers: list[Ingestor] = []
    for name in cfg.ingestion.providers:
        if name == "source_file":
            providers.append(SourceFileIngestor())
        elif name == "folder":
            providers.append(FolderIngestor(cfg.ingestion.folders))
        else:
            raise ValueError(f"unknown ingestion provider {name!r} (available: {sorted(PROVIDERS)})")
    return ChainIngestor(providers)
