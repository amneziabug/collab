"""SQLite persistence: videos, their state machine, AI decisions and an event log."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# Lifecycle of a video through the pipeline.
STATUSES = (
    "discovered",   # metadata ingested from source
    "rejected",     # failed rights check or selection criteria
    "selected",     # passed AI analysis + ranking
    "processed",    # video file transcoded to Shorts format
    "metadata_ready",
    "scheduled",
    "uploading",
    "uploaded",
    "failed",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source           TEXT NOT NULL,
    source_id        TEXT NOT NULL,
    title            TEXT,
    description      TEXT,
    creator          TEXT,
    license          TEXT,
    rights_holder    TEXT,
    attribution      TEXT,
    file_path        TEXT,
    duration_seconds REAL,
    views            INTEGER DEFAULT 0,
    likes            INTEGER DEFAULT 0,
    comments         INTEGER DEFAULT 0,
    shares           INTEGER DEFAULT 0,
    hashtags         TEXT,              -- JSON list
    status           TEXT NOT NULL DEFAULT 'discovered',
    status_reason    TEXT,
    engagement_score REAL,
    relevance_score  REAL,
    final_score      REAL,
    topic            TEXT,
    processed_path   TEXT,
    content_hash     TEXT,              -- sha256 of the source file, for duplicate detection
    has_stats        INTEGER NOT NULL DEFAULT 1,
    visual_summary   TEXT,              -- what a vision model saw in the frames
    publish_at       TEXT,              -- when the video goes/went public (UTC ISO)
    yt_title         TEXT,
    yt_description   TEXT,
    yt_tags          TEXT,              -- JSON list
    scheduled_at     TEXT,              -- ISO-8601 UTC
    youtube_video_id TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (source, source_id)
);

CREATE TABLE IF NOT EXISTS ai_decisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id   INTEGER NOT NULL REFERENCES videos(id),
    stage      TEXT NOT NULL,          -- 'analysis' | 'metadata'
    model      TEXT NOT NULL,
    prompt     TEXT NOT NULL,
    response   TEXT NOT NULL,          -- raw JSON returned by the model
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id   INTEGER REFERENCES videos(id),
    event      TEXT NOT NULL,
    detail     TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_scheduled ON videos(scheduled_at);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(videos)")}
        for name, ddl in (("content_hash", "TEXT"), ("has_stats", "INTEGER NOT NULL DEFAULT 1"),
                          ("visual_summary", "TEXT"), ("publish_at", "TEXT")):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE videos ADD COLUMN {name} {ddl}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # --- videos -------------------------------------------------------------

    def upsert_discovered(self, item: dict[str, Any]) -> tuple[int, bool]:
        """Insert a newly discovered video. Returns (id, created)."""
        row = self.conn.execute(
            "SELECT id FROM videos WHERE source = ? AND source_id = ?",
            (item["source"], item["source_id"]),
        ).fetchone()
        if row:
            # A file the user added after an earlier run (e.g. a TikTok export) is picked up.
            if item.get("file_path"):
                current = self.get(row["id"])
                if not current["file_path"]:
                    self.update(row["id"], file_path=item["file_path"])
                    self.log_event(row["id"], "file_found", item["file_path"])
            return row["id"], False
        now = utcnow()
        cols = [
            "source", "source_id", "title", "description", "creator", "license",
            "rights_holder", "attribution", "file_path", "duration_seconds",
            "views", "likes", "comments", "shares",
        ]
        values = [item.get(c) for c in cols]
        # Missing stats count as 0 (an explicit NULL would bypass the column default).
        for i, c in enumerate(cols):
            if c in ("views", "likes", "comments", "shares") and values[i] is None:
                values[i] = 0
        cols += ["hashtags", "has_stats", "created_at", "updated_at"]
        values += [json.dumps(item.get("hashtags", [])), int(item.get("has_stats", True)), now, now]
        with self.tx() as c:
            cur = c.execute(
                f"INSERT INTO videos ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                values,
            )
        return cur.lastrowid, True

    def get(self, video_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        if row is None:
            raise KeyError(video_id)
        return row

    def by_status(self, status: str, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM videos WHERE status = ? ORDER BY COALESCE(final_score, 0) DESC, id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return list(self.conn.execute(sql, (status,)))

    def update(self, video_id: int, **fields: Any) -> None:
        if not fields:
            return
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError(f"invalid status {fields['status']!r}")
        for key in ("yt_tags", "hashtags"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = json.dumps(fields[key])
        fields["updated_at"] = utcnow()
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE videos SET {assignments} WHERE id = ?", (*fields.values(), video_id))

    def set_status(self, video_id: int, status: str, reason: str | None = None, **fields) -> None:
        self.update(video_id, status=status, status_reason=reason, **fields)
        self.log_event(video_id, f"status:{status}", reason)

    def due_uploads(self, now_iso: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM videos WHERE status = 'scheduled' AND scheduled_at <= ? "
            "ORDER BY scheduled_at, id",
            (now_iso,),
        ))

    def find_by_hash(self, content_hash: str, exclude_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM videos WHERE content_hash = ? AND id != ? "
            "AND status IN ('processed', 'metadata_ready', 'scheduled', 'uploading', 'uploaded') "
            "ORDER BY id LIMIT 1",
            (content_hash, exclude_id),
        ).fetchone()

    def latest_publish_at(self) -> str | None:
        row = self.conn.execute(
            "SELECT MAX(publish_at) AS t FROM videos WHERE status IN ('scheduled', 'uploading', 'uploaded')"
        ).fetchone()
        return row["t"]

    def scheduled_times(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT scheduled_at FROM videos WHERE scheduled_at IS NOT NULL "
            "AND status IN ('scheduled', 'uploading', 'uploaded')"
        )
        return {r["scheduled_at"] for r in rows}

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM videos GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    # --- logs ---------------------------------------------------------------

    def log_ai_decision(self, video_id: int, stage: str, model: str, prompt: str, response: Any) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO ai_decisions (video_id, stage, model, prompt, response, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (video_id, stage, model, prompt, json.dumps(response, ensure_ascii=False), utcnow()),
            )

    def log_event(self, video_id: int | None, event: str, detail: Any = None) -> None:
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False, default=str)
        with self.tx() as c:
            c.execute(
                "INSERT INTO events (video_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
                (video_id, event, detail, utcnow()),
            )

    def recent_events(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)))
