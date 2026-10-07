"""Pipeline orchestration. Each stage reads videos in one status and moves them on,
so stages are idempotent and can be re-run (or run separately from cron)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import scheduler
from .ai import AIError, analyze_video, generate_metadata
from .ai.client import AIClient
from .config import Config
from .db import Database
from .processing import ProcessingError, process_video
from .ranking import final_score
from .sources import VideoSource, check_rights
from .youtube.uploader import QuotaExceededError, UploadError

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


class Pipeline:
    def __init__(self, cfg: Config, db: Database, ai_factory: Callable[[], AIClient],
                 uploader_factory: Callable[[], object] | None = None):
        self.cfg = cfg
        self.db = db
        self._ai_factory = ai_factory
        self._ai: AIClient | None = None
        self._uploader_factory = uploader_factory
        self._uploader = None

    @property
    def ai(self) -> AIClient:
        if self._ai is None:
            self._ai = self._ai_factory()
        return self._ai

    # 1. Discovery ------------------------------------------------------------
    def discover(self, source: VideoSource) -> int:
        new = 0
        for item in source.discover():
            data = item.to_dict()
            video_id, created = self.db.upsert_discovered(data)
            if not created:
                continue
            new += 1
            self.db.log_event(video_id, "discovered", {"source": item.source, "source_id": item.source_id})
            ok, reason = check_rights(data, self.cfg.selection.allowed_licenses)
            if not ok:
                self.db.set_status(video_id, "rejected", f"rights: {reason}")
                log.info("Rejected %s (%s): %s", item.source_id, item.title, reason)
        log.info("Discovery: %d new video(s) from %s", new, source.name)
        return new

    # 2. AI analysis + ranking -------------------------------------------------
    def analyze_and_rank(self) -> int:
        sel = self.cfg.selection
        selected = 0
        candidates = self.db.by_status("discovered")
        scored = []
        for video in candidates:
            if video["views"] < sel.min_views:
                self.db.set_status(video["id"], "rejected", f"views {video['views']} < {sel.min_views}")
                continue
            try:
                prompt, result = analyze_video(self.ai, video, sel.topics, sel.min_relevance)
            except AIError as exc:
                self._fail(video, "analysis", exc)
                continue
            self.db.log_ai_decision(video["id"], "analysis", self.ai.model, prompt, result)
            engagement, score = final_score(dict(video), result["relevance"], result["educational_value"])
            self.db.update(video["id"], topic=result["topic"], relevance_score=result["relevance"],
                           engagement_score=engagement, final_score=score)
            if result["content_flags"]:
                self.db.set_status(video["id"], "rejected", f"AI flags: {result['content_flags']}")
            elif not result["recommend"] or result["relevance"] < sel.min_relevance:
                self.db.set_status(video["id"], "rejected",
                                   f"relevance {result['relevance']:.2f}: {result['reasoning']}")
            else:
                scored.append((score, video["id"], result["reasoning"]))

        scored.sort(reverse=True)
        for rank, (score, video_id, reasoning) in enumerate(scored, start=1):
            if rank <= sel.max_candidates_per_run:
                self.db.set_status(video_id, "selected", f"rank {rank}, score {score:.3f}: {reasoning}")
                selected += 1
            else:
                self.db.set_status(video_id, "rejected", f"rank {rank} beyond max_candidates_per_run")
        log.info("Analysis: %d analysed, %d selected", len(candidates), selected)
        return selected

    # 3. Processing ------------------------------------------------------------
    def process(self) -> int:
        p = self.cfg.processing
        done = 0
        for video in self.db.by_status("selected"):
            try:
                out, duration = process_video(
                    Path(video["file_path"] or ""), self.cfg.paths.processed_dir,
                    max_duration=p.max_duration_seconds, width=p.width, height=p.height,
                    bitrate=p.video_bitrate,
                )
            except ProcessingError as exc:
                self._fail(video, "processing", exc)
                continue
            self.db.set_status(video["id"], "processed", None,
                               processed_path=str(out), duration_seconds=duration)
            done += 1
        log.info("Processing: %d video(s) processed", done)
        return done

    # 4. Metadata ---------------------------------------------------------------
    def generate_metadata(self) -> int:
        done = 0
        for video in self.db.by_status("processed"):
            try:
                prompt, meta = generate_metadata(self.ai, video)
            except AIError as exc:
                self._fail(video, "metadata", exc)
                continue
            self.db.log_ai_decision(video["id"], "metadata", self.ai.model, prompt, meta)
            self.db.set_status(video["id"], "metadata_ready", None, yt_title=meta["title"],
                               yt_description=meta["description"], yt_tags=meta["tags"])
            done += 1
        log.info("Metadata: generated for %d video(s)", done)
        return done

    # 5. Scheduling ---------------------------------------------------------------
    def schedule(self, now: datetime | None = None) -> int:
        s = self.cfg.schedule
        now = now or datetime.now(timezone.utc)
        slots = scheduler.free_slots(now, self.db.scheduled_times(), s.uploads_per_day,
                                     s.window_start, s.window_end, s.timezone, s.days_ahead)
        ready = self.db.by_status("metadata_ready")
        for video, slot in zip(ready, slots):
            self.db.set_status(video["id"], "scheduled", None, scheduled_at=slot)
            log.info("Scheduled #%d %r at %s", video["id"], video["yt_title"], slot)
        if len(ready) > len(slots):
            log.info("%d video(s) waiting for free slots", len(ready) - len(slots))
        return min(len(ready), len(slots))

    # 6. Upload -------------------------------------------------------------------
    def upload_due(self, now: datetime | None = None, dry_run: bool = False) -> int:
        now = now or datetime.now(timezone.utc)
        due = self.db.due_uploads(scheduler.to_iso(now))
        uploaded = 0
        for video in due:
            if dry_run:
                log.info("[dry-run] would upload #%d %r (private)", video["id"], video["yt_title"])
                continue
            self.db.set_status(video["id"], "uploading", None, attempts=video["attempts"] + 1)
            try:
                if self._uploader is None:
                    if self._uploader_factory is None:
                        raise UploadError("no uploader configured")
                    self._uploader = self._uploader_factory()
                yt_id = self._uploader.upload(
                    Path(video["processed_path"]), title=video["yt_title"],
                    description=video["yt_description"], tags=json.loads(video["yt_tags"] or "[]"),
                    category_id=self.cfg.youtube.category_id, language=self.cfg.youtube.default_language,
                )
            except QuotaExceededError as exc:
                # Put it back and stop for today; the next run will pick it up.
                self.db.set_status(video["id"], "scheduled", "quota exceeded", last_error=str(exc))
                log.warning("YouTube quota exceeded; stopping uploads for this run")
                break
            except Exception as exc:  # noqa: BLE001 - every failure must be recorded
                video = self.db.get(video["id"])
                if video["attempts"] < MAX_ATTEMPTS:
                    self.db.set_status(video["id"], "scheduled", f"retry after error: {exc}",
                                       last_error=str(exc))
                else:
                    self._fail(video, "upload", exc)
                log.exception("Upload of #%d failed", video["id"])
                continue
            self.db.set_status(video["id"], "uploaded", "private", youtube_video_id=yt_id)
            log.info("Uploaded #%d as https://youtu.be/%s (private)", video["id"], yt_id)
            uploaded += 1
        return uploaded

    # -------------------------------------------------------------------------
    def run_all(self, source: VideoSource) -> dict[str, int]:
        return {
            "discovered": self.discover(source),
            "selected": self.analyze_and_rank(),
            "processed": self.process(),
            "metadata": self.generate_metadata(),
            "scheduled": self.schedule(),
        }

    def _fail(self, video, stage: str, exc: Exception) -> None:
        log.error("%s failed for #%d: %s", stage, video["id"], exc)
        self.db.set_status(video["id"], "failed", f"{stage}: {exc}", last_error=str(exc))
