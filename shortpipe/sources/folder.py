"""A plain folder of videos you made yourself, e.g. a Windows folder seen from WSL
(C:\\Users\\<you>\\Videos\\shortpipe  ->  /mnt/c/Users/<you>/Videos/shortpipe).

Drop a video in, and the next run picks it up: no manifest needed. The title starts
from the filename ("my_physics-demo.mp4" -> "My physics demo") plus any title or
comment stored inside the file; the AI then writes the YouTube metadata.

Everything in this folder is declared as yours via `folder.owner` in config.toml;
leave it empty and the videos are rejected by the rights check.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Iterator

from .base import SourceItem, VideoSource

log = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}


def title_from_filename(path: Path) -> str:
    text = re.sub(r"[_\-.]+", " ", path.stem)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:1].upper() + text[1:] if text else path.stem


def read_file_tags(path: Path) -> tuple[dict, float | None]:
    """Container tags (title, comment, description...) and duration, via ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        fmt = json.loads(result.stdout or "{}").get("format", {})
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}, None
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    duration = float(fmt["duration"]) if fmt.get("duration") else None
    return tags, duration


class FolderSource(VideoSource):
    name = "folder"

    def __init__(self, folder: Path, owner: str, recursive: bool = False):
        self.folder = Path(folder)
        self.owner = owner.strip()
        self.recursive = recursive

    def discover(self) -> Iterator[SourceItem]:
        if not self.folder.is_dir():
            raise FileNotFoundError(f"Video folder not found: {self.folder} "
                                    "(set [folder] path in config.toml)")
        if not self.owner:
            log.warning("[folder] owner is empty in config.toml: set it to your name to confirm "
                        "you made these videos, otherwise they are rejected")
        pattern = "**/*" if self.recursive else "*"
        files = sorted(p for p in self.folder.glob(pattern)
                       if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS)
        for path in files:
            tags, duration = read_file_tags(path)
            description = tags.get("description") or tags.get("comment") or tags.get("synopsis") or ""
            stat = path.stat()
            yield SourceItem(
                source=self.name,
                # Name + size: a re-saved or edited file counts as a new video, while the
                # content hash check later still catches an identical copy under a new name.
                source_id=f"{path.relative_to(self.folder).as_posix()}:{stat.st_size}",
                title=tags.get("title") or title_from_filename(path),
                description=description,
                creator=self.owner,
                license="owned",
                rights_holder=self.owner,
                file_path=str(path),
                duration_seconds=duration,
                has_stats=False,
                hashtags=re.findall(r"#(\w+)", description),
            )
        log.info("Folder: %d video file(s) in %s", len(files), self.folder)
