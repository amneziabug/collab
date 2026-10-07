"""Local/authorized test dataset described by a JSON manifest.

Manifest format (data/dataset/manifest.json):

    {
      "videos": [
        {
          "id": "clip-001",
          "file": "clip-001.mp4",              # relative to the manifest
          "title": "Why the sky is blue",
          "description": "...",
          "creator": "Your Name",
          "license": "owned",                  # owned | cc0 | cc-by | written-permission
          "rights_holder": "Your Name",
          "attribution": "",                   # required for cc-by
          "stats": {"views": 12000, "likes": 900, "comments": 40, "shares": 25},
          "hashtags": ["physics", "science"]
        }
      ]
    }

The stats can come from your own analytics export or be synthetic test values.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterator

from .base import SourceItem, VideoSource

log = logging.getLogger(__name__)


class LocalDatasetSource(VideoSource):
    name = "local"

    def __init__(self, manifest_path: Path):
        self.manifest_path = Path(manifest_path)

    def discover(self) -> Iterator[SourceItem]:
        if not self.manifest_path.exists():
            raise FileNotFoundError(
                f"Dataset manifest not found: {self.manifest_path}. "
                "Run `python -m shortpipe make-demo-dataset` or create one."
            )
        data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        base = self.manifest_path.parent
        for entry in data.get("videos", []):
            try:
                stats = entry.get("stats", {})
                file_path = base / entry["file"] if entry.get("file") else None
                yield SourceItem(
                    source=self.name,
                    source_id=str(entry["id"]),
                    title=entry.get("title", ""),
                    description=entry.get("description", ""),
                    creator=entry.get("creator", ""),
                    license=str(entry.get("license", "")).lower().strip(),
                    rights_holder=entry.get("rights_holder", ""),
                    attribution=entry.get("attribution", ""),
                    file_path=str(file_path) if file_path else None,
                    duration_seconds=entry.get("duration_seconds"),
                    views=int(stats.get("views", 0)),
                    likes=int(stats.get("likes", 0)),
                    comments=int(stats.get("comments", 0)),
                    shares=int(stats.get("shares", 0)),
                    hashtags=list(entry.get("hashtags", [])),
                )
            except (KeyError, ValueError, TypeError) as exc:
                log.warning("Skipping malformed manifest entry %r: %s", entry.get("id"), exc)
