"""Source abstraction. New sources (e.g. your own channel, a licensed stock API)
implement `VideoSource.discover()` and return `SourceItem`s with rights info."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Iterable


@dataclass
class SourceItem:
    source: str
    source_id: str
    title: str
    description: str = ""
    creator: str = ""
    license: str = ""          # e.g. owned | cc0 | cc-by | written-permission
    rights_holder: str = ""
    attribution: str = ""      # credit line to include in the YouTube description
    file_path: str | None = None
    duration_seconds: float | None = None
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    hashtags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class VideoSource(ABC):
    name: str

    @abstractmethod
    def discover(self) -> Iterable[SourceItem]:
        """Yield candidate videos with their metadata and rights information."""
