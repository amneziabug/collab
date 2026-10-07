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
    provider: str = "ollama"  # "ollama" | "claude" | "offline"
    max_retries: int = 3
    # Ollama (local model)
    ollama_model: str = "llama3.2:3b"
    ollama_url: str = "http://localhost:11434"
    ollama_timeout: int = 300
    ollama_vision_model: str = ""  # optional, e.g. "gemma3:4b": describes frames of each video
    vision_frames: int = 3
    # Claude API
    model: str = "claude-opus-5-5"
    effort: str = "medium"  # low | medium | high | xhigh | max


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
    title: str = ""             # fixed title for every upload (e.g. "#fyp #viral"); "" = AI-written
    description: str = "ai"     # "ai" = AI-written description, "none" = leave it empty
    privacy: str = "private"    # "private" | "unlisted" | "public"


@dataclass
class SourcesConfig:
    enabled: list[str] = field(default_factory=lambda: ["local"])  # "local", "tiktok", "folder"


@dataclass
class TikTokConfig:
    client_key: str = ""        # or env TIKTOK_CLIENT_KEY
    client_secret: str = ""     # or env TIKTOK_CLIENT_SECRET (prefer the env var)
    video_dir: Path = Path("data/tiktok")
    token_file: Path = Path("secrets/tiktok_token.json")
    redirect_port: int = 8765
    max_videos: int = 50


@dataclass
class FolderConfig:
    path: Path = Path("data/my_videos")  # e.g. "/mnt/c/Users/<you>/Videos/shortpipe" on WSL
    owner: str = ""             # your name: declares you made/own every video in this folder
    recursive: bool = False     # also look in sub-folders
    auto_approve: bool = True   # schedule every video here (unless the AI flags a problem)


@dataclass
class Config:
    paths: Paths = field(default_factory=Paths)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    tiktok: TikTokConfig = field(default_factory=TikTokConfig)
    folder: FolderConfig = field(default_factory=FolderConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    youtube: YouTubeConfig = field(default_factory=YouTubeConfig)


# Keys from older config files that are now ignored instead of rejected.
_REMOVED_KEYS = {("ai", "temperature")}


def _apply(section: str, section_obj, values: dict) -> None:
    for key, value in values.items():
        if (section, key) in _REMOVED_KEYS:
            continue
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
            _apply(section, getattr(cfg, section), values)

    cfg.tiktok.client_key = os.environ.get("TIKTOK_CLIENT_KEY", cfg.tiktok.client_key)
    cfg.tiktok.client_secret = os.environ.get("TIKTOK_CLIENT_SECRET", cfg.tiktok.client_secret)
    unknown = set(cfg.sources.enabled) - {"local", "tiktok", "folder"}
    if unknown:
        raise ValueError(f"sources.enabled has unknown source(s): {sorted(unknown)}")
    if host := os.environ.get("OLLAMA_HOST"):
        cfg.ai.ollama_url = host
    if model := os.environ.get("CLAUDE_MODEL"):
        cfg.ai.model = model
    if provider := os.environ.get("SHORTPIPE_AI_PROVIDER"):
        cfg.ai.provider = provider

    if cfg.ai.provider == "openai":
        raise ValueError('ai.provider "openai" is no longer supported; use "ollama", "claude" or "offline"')
    if cfg.ai.effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError("ai.effort must be one of low, medium, high, xhigh, max")
    if cfg.youtube.privacy not in {"private", "unlisted", "public"}:
        raise ValueError('youtube.privacy must be "private", "unlisted" or "public"')
    if cfg.youtube.description not in {"ai", "none"}:
        raise ValueError('youtube.description must be "ai" or "none"')
    if len(cfg.youtube.title) > 100 or any(c in cfg.youtube.title for c in "<>"):
        raise ValueError("youtube.title must be at most 100 characters and contain no < or >")
    if not 1 <= cfg.schedule.uploads_per_day <= 6:
        # The default YouTube Data API quota (10,000 units/day) allows ~6 uploads.
        raise ValueError("schedule.uploads_per_day must be between 1 and 6")
    return cfg
