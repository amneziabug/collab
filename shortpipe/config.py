"""Configuration loading: TOML file + environment overrides."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Paths:
    database: Path = Path("data/pipeline.db")
    dataset_manifest: Path = Path("data/dataset/manifest.json")
    processed_dir: Path = Path("data/processed")
    log_dir: Path = Path("logs")
    client_secrets: Path = Path("secrets/client_secret.json")
    token_file: Path = Path("secrets/youtube_token.json")


@dataclass
class AIConfig:
    model: str = "gpt-4.1-mini"
    temperature: float = 0.4
    max_retries: int = 3
    provider: str = "openai"  # "openai" | "offline"


@dataclass
class SelectionConfig:
    topics: list[str] = field(default_factory=lambda: ["science", "programming"])
    min_views: int = 1000
    min_relevance: float = 0.5
    max_candidates_per_run: int = 10
    allowed_licenses: list[str] = field(
        default_factory=lambda: ["owned", "cc0", "cc-by", "written-permission"]
    )


@dataclass
class ProcessingConfig:
    max_duration_seconds: int = 60
    width: int = 1080
    height: int = 1920
    video_bitrate: str = "4M"


@dataclass
class ScheduleConfig:
    uploads_per_day: int = 5
    window_start: str = "09:00"
    window_end: str = "21:00"
    timezone: str = "UTC"
    days_ahead: int = 3


@dataclass
class YouTubeConfig:
    category_id: str = "27"
    default_language: str = "en"


@dataclass
class Config:
    paths: Paths = field(default_factory=Paths)
    ai: AIConfig = field(default_factory=AIConfig)
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    youtube: YouTubeConfig = field(default_factory=YouTubeConfig)

    @property
    def openai_api_key(self) -> str | None:
        return os.environ.get("OPENAI_API_KEY")


def _apply(section_obj, values: dict) -> None:
    for key, value in values.items():
        if not hasattr(section_obj, key):
            raise ValueError(f"Unknown config key: {type(section_obj).__name__}.{key}")
        current = getattr(section_obj, key)
        if isinstance(current, Path):
            value = Path(value)
        setattr(section_obj, key, value)


def load_config(path: str | Path | None = None) -> Config:
    cfg = Config()
    path = Path(path or os.environ.get("SHORTPIPE_CONFIG", "config.toml"))
    if path.exists():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        for section, values in raw.items():
            if not hasattr(cfg, section):
                raise ValueError(f"Unknown config section: [{section}]")
            _apply(getattr(cfg, section), values)

    if model := os.environ.get("OPENAI_MODEL"):
        cfg.ai.model = model
    if provider := os.environ.get("SHORTPIPE_AI_PROVIDER"):
        cfg.ai.provider = provider

    if not 1 <= cfg.schedule.uploads_per_day <= 6:
        # The default YouTube Data API quota (10,000 units/day) allows ~6 uploads.
        raise ValueError("schedule.uploads_per_day must be between 1 and 6")
    return cfg
