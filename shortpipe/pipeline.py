"""Pipeline orchestration. Each stage reads videos in one status and moves them on,
so stages are idempotent and can be re-run (or run separately from cron)."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import scheduler
from .ai import AIConfigError, AIError, analyze_video, generate_metadata
from .ai.client import AIClient
from .config import Config
from .db import Database
from .processing import ProcessingError, process_video
from .ranking import final_score
from .sources import TikTokAuthError, TikTokError, VideoSource, check_rights
from .youtube.uploader import QuotaExceededError, UploadError, YouTubeAuthError

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
        try:
            items = list(source.discover())
        except TikTokAuthError:
            raise  # login problem: stop and tell the user to re-authenticate
        except (TikTokError, OSError) as exc:
            log.error("Discovery from %s failed: %s", source.name, exc)
            self.db.log_event(None, "discovery_failed", {"source": source.name, "error": str(exc)})
            return 0
        for item in items:
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
        if candidates:
            self.ai  # fail fast on config errors (e.g. missing API key) without marking videos failed
        scored = []
        for n, video in enumerate(candidates, start=1):
            if video["views"] < sel.min_views:
                self.db.set_status(video["id"], "rejected", f"views {video['views']} < {sel.min_views}")
                log.info("[%d/%d] #%d %r rejected: only %d views", n, len(candidates),
                         video["id"], video["title"], video["views"])
                continue
            log.info("[%d/%d] Analysing #%d %r with %s ...", n, len(candidates),
                     video["id"], video["title"], self.ai.model)
            started = time.monotonic()
            try:
                prompt, result = analyze_video(self.ai, video, sel.topics, sel.min_relevance)
            except AIConfigError:
                raise  # provider unusable: stop without marking videos failed
            except AIError as exc:
                self._fail(video, "analysis", exc)
                continue
            self.db.log_ai_decision(video["id"], "analysis", self.ai.model, prompt, result)
            log.info("      topic=%s relevance=%.2f recommend=%s (%.0fs)", result["topic"],
                     result["relevance"], result["recommend"], time.monotonic() - started)
            engagement, score = final_score(dict(video), result["relevance"], result["educational_value"])
            self.db.update(video["id"], topic=result["topic"], relevance_score=result["relevance"],
                           engagement_score=engagement, final_score=score)
            if result["content_flags"]:
                self.db.set_status(video["id"], "rejected", f"AI flags: {result['content_flags']}")
            elif result["relevance"] < sel.min_relevance:
                # Selection uses the numeric score only; the model's own yes/no is logged but not
                # trusted, because small local models often contradict their scores.
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
        selected = self.db.by_status("selected")
        for n, video in enumerate(selected, start=1):
            src = Path(video["file_path"] or "")
            if not video["file_path"] or not src.is_file():
                # Not an error: e.g. a TikTok video whose export hasn't been copied in yet.
                if video["status_reason"] != "waiting for local file":
                    self.db.set_status(video["id"], "selected", "waiting for local file")
                log.warning("[%d/%d] #%d %r: no local video file yet (%s); will retry next run",
                            n, len(selected), video["id"], video["title"], video["file_path"] or "not found")
                continue
            content_hash = file_sha256(src)
            duplicate = self.db.find_by_hash(content_hash, video["id"])
            if duplicate:
                self.db.set_status(video["id"], "rejected", f"duplicate of #{duplicate['id']}",
                                   content_hash=content_hash)
                log.info("[%d/%d] #%d %r rejected: same file as #%d", n, len(selected),
                         video["id"], video["title"], duplicate["id"])
                continue
            self.db.update(video["id"], content_hash=content_hash)
            log.info("[%d/%d] Processing #%d %r ...", n, len(selected), video["id"], video["title"])
            try:
                out, duration = process_video(
                    src, self.cfg.paths.processed_dir,
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
        pending = self.db.by_status("processed")
        if pending:
            self.ai  # fail fast on config errors without marking videos failed
        for n, video in enumerate(pending, start=1):
            log.info("[%d/%d] Writing metadata for #%d %r ...", n, len(pending), video["id"], video["title"])
            started = time.monotonic()
            try:
                prompt, meta = generate_metadata(self.ai, video)
            except AIConfigError:
                raise
            except AIError as exc:
                self._fail(video, "metadata", exc)
                continue
            self.db.log_ai_decision(video["id"], "metadata", self.ai.model, prompt, meta)
            log.info("      title: %s (%.0fs)", meta["title"], time.monotonic() - started)
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
            except YouTubeAuthError as exc:
                # Not the video's fault: undo the attempt and stop until the user logs in again.
                self.db.set_status(video["id"], "scheduled", "waiting for YouTube login",
                                   attempts=video["attempts"], last_error=str(exc))
                log.error("%s", exc)
                break
            except QuotaExceededError as exc:
                # Put it back and stop for today; the next run will pick it up.
                self.db.set_status(video["id"], "scheduled", "quota exceeded",
                                   attempts=video["attempts"], last_error=str(exc))
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

    # Recovery ------------------------------------------------------------------
    # Status a failed video returns to, keyed by the stage that failed.
    RETRY_STATUS = {"analysis": "discovered", "processing": "selected",
                    "metadata": "processed", "upload": "metadata_ready"}

    def retry_failed(self) -> int:
        """Send failed videos back to the stage that failed so the next run retries them."""
        count = 0
        for video in self.db.by_status("failed"):
            stage = (video["status_reason"] or "").split(":", 1)[0]
            target = self.RETRY_STATUS.get(stage)
            if target is None:
                log.warning("#%d: unknown failed stage %r, skipping", video["id"], stage)
                continue
            self.db.set_status(video["id"], target, f"retry after {stage} failure",
                               attempts=0, scheduled_at=None, last_error=None)
            count += 1
        log.info("Retry: %d failed video(s) reset", count)
        return count

    # -------------------------------------------------------------------------
    def run_all(self, sources: VideoSource | list[VideoSource]) -> dict[str, int]:
        if not isinstance(sources, list):
            sources = [sources]
        return {
            "discovered": sum(self.discover(source) for source in sources),
            "selected": self.analyze_and_rank(),
            "processed": self.process(),
            "metadata": self.generate_metadata(),
            "scheduled": self.schedule(),
        }

    def _fail(self, video, stage: str, exc: Exception) -> None:
        log.error("%s failed for #%d: %s", stage, video["id"], exc)
        self.db.set_status(video["id"], "failed", f"{stage}: {exc}", last_error=str(exc))
