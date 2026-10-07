"""Command-line interface: python -m shortpipe <command>."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import time
from datetime import datetime, timezone

from .ai import AIError, make_ai_client
from .config import load_config
from .db import Database
from .logging_setup import setup_logging
from .pipeline import Pipeline
from .sources import FolderSource, LocalDatasetSource, TikTokAuth, TikTokAuthError, TikTokDisplaySource

log = logging.getLogger("shortpipe")


def _uploader_factory(cfg):
    def factory():
        from .youtube import YouTubeUploader, get_credentials
        creds = get_credentials(cfg.paths.client_secrets, cfg.paths.token_file, interactive=False)
        return YouTubeUploader(creds)
    return factory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shortpipe", description=__doc__)
    parser.add_argument("-c", "--config", help="path to config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("make-demo-dataset", help="create synthetic self-owned test videos")
    t = sub.add_parser("tiktok-auth", help="log in to your TikTok account (Display API)")
    t.add_argument("--no-browser", action="store_true", help="print URL instead of opening a browser")
    a = sub.add_parser("auth", help="run YouTube OAuth flow and store token")
    a.add_argument("--no-browser", action="store_true", help="print URL instead of opening a browser")
    a.add_argument("--port", type=int, default=8080)
    for name, help_ in [("discover", "ingest metadata from the dataset"),
                        ("analyze", "AI analysis + ranking"),
                        ("process", "transcode selected videos"),
                        ("metadata", "AI-generate YouTube metadata"),
                        ("schedule", "assign upload slots"),
                        ("run", "discover -> analyze -> process -> metadata -> schedule")]:
        sub.add_parser(name, help=help_)
    sub.add_parser("retry-failed", help="send failed videos back to the stage that failed")
    now = sub.add_parser("upload-now", help="prepare all videos and upload them immediately (no schedule)")
    now.add_argument("--dry-run", action="store_true", help="show what would be uploaded")
    up = sub.add_parser("upload-due", help="upload videos whose slot has arrived (cron-friendly)")
    up.add_argument("--dry-run", action="store_true")
    d = sub.add_parser("daemon", help="loop: run pipeline + upload due videos periodically")
    d.add_argument("--interval", type=int, default=300, help="seconds between checks")
    d.add_argument("--dry-run", action="store_true")
    st = sub.add_parser("status", help="show counts, schedule and recent events")
    st.add_argument("--events", type=int, default=15)

    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (ValueError, OSError) as exc:
        print(f"Config error: {exc}")
        return 2
    setup_logging(cfg.paths.log_dir, args.verbose)

    if args.command == "make-demo-dataset":
        from .demo_dataset import make_demo_dataset
        from .processing import ProcessingError
        try:
            path = make_demo_dataset(cfg.paths.dataset_manifest.parent)
        except ProcessingError as exc:
            log.error("%s", exc)
            return 1
        print(f"Demo dataset written to {path}")
        return 0

    if args.command == "auth":
        from .youtube import get_credentials
        get_credentials(cfg.paths.client_secrets, cfg.paths.token_file,
                        port=args.port, open_browser=not args.no_browser)
        print(f"Authorized. Token stored in {cfg.paths.token_file}")
        return 0

    if args.command == "tiktok-auth":
        try:
            _tiktok_auth(cfg).login(open_browser=not args.no_browser)
        except TikTokAuthError as exc:
            log.error("%s", exc)
            return 1
        print(f"Logged in to TikTok. Token stored in {cfg.tiktok.token_file}")
        return 0

    db = Database(cfg.paths.database)
    pipe = Pipeline(cfg, db, ai_factory=lambda: make_ai_client(cfg),
                    uploader_factory=_uploader_factory(cfg))
    try:
        sources = _sources(cfg)
    except TikTokAuthError as exc:
        log.error("%s", exc)
        db.close()
        return 1

    try:
        if args.command == "discover":
            for source in sources:
                pipe.discover(source)
        elif args.command == "analyze":
            pipe.analyze_and_rank()
        elif args.command == "process":
            pipe.process()
        elif args.command == "metadata":
            pipe.generate_metadata()
        elif args.command == "schedule":
            pipe.schedule()
        elif args.command == "run":
            print(json.dumps(pipe.run_all(sources), indent=2))
        elif args.command == "upload-now":
            print(json.dumps(pipe.upload_now(sources, dry_run=args.dry_run), indent=2))
        elif args.command == "upload-due":
            pipe.upload_due(dry_run=args.dry_run)
        elif args.command == "daemon":
            _daemon(pipe, sources, args.interval, args.dry_run)
        elif args.command == "retry-failed":
            pipe.retry_failed()
        elif args.command == "status":
            _status(db, args.events)
    except (AIError, FileNotFoundError, TikTokAuthError) as exc:
        log.error("%s", exc)
        return 1
    finally:
        db.close()
    return 0


def _tiktok_auth(cfg) -> TikTokAuth:
    return TikTokAuth(cfg.tiktok.client_key, cfg.tiktok.client_secret, cfg.tiktok.token_file,
                      cfg.tiktok.redirect_port)


def _sources(cfg) -> list:
    sources = []
    if "local" in cfg.sources.enabled:
        sources.append(LocalDatasetSource(cfg.paths.dataset_manifest))
    if "folder" in cfg.sources.enabled:
        sources.append(FolderSource(cfg.folder.path, cfg.folder.owner, cfg.folder.recursive))
    if "tiktok" in cfg.sources.enabled:
        sources.append(TikTokDisplaySource(_tiktok_auth(cfg), cfg.tiktok.video_dir,
                                           cfg.tiktok.max_videos))
    return sources


def _daemon(pipe: Pipeline, sources, interval: int, dry_run: bool) -> None:
    stop = False

    def _handle(signum, _frame):
        nonlocal stop
        log.info("Received signal %s, stopping after current cycle", signum)
        stop = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    log.info("Daemon started (interval %ss, dry_run=%s)", interval, dry_run)
    while not stop:
        try:
            pipe.run_all(sources)
            pipe.upload_due(dry_run=dry_run)
        except Exception:  # noqa: BLE001 - keep the daemon alive; error is logged
            log.exception("Pipeline cycle failed")
        for _ in range(interval):
            if stop:
                break
            time.sleep(1)


def _status(db: Database, n_events: int) -> None:
    print("Videos by status:")
    for status, n in sorted(db.counts().items()):
        print(f"  {status:<15} {n}")
    upcoming = db.conn.execute(
        "SELECT id, yt_title, scheduled_at, status, youtube_video_id FROM videos "
        "WHERE scheduled_at IS NOT NULL ORDER BY scheduled_at"
    ).fetchall()
    if upcoming:
        print("\nSchedule (UTC):")
        for r in upcoming:
            extra = f" -> youtu.be/{r['youtube_video_id']}" if r["youtube_video_id"] else ""
            print(f"  {r['scheduled_at']}  #{r['id']:<4} {r['status']:<10} {r['yt_title']}{extra}")
    waiting = db.conn.execute(
        "SELECT id, source, source_id, title FROM videos "
        "WHERE status = 'selected' AND status_reason = 'waiting for local file' ORDER BY id"
    ).fetchall()
    if waiting:
        print("\nWaiting for video files (add them, then `run` again):")
        for r in waiting:
            hint = f"data/tiktok/{r['source_id']}.mp4" if r["source"] == "tiktok" else "file in manifest"
            print(f"  #{r['id']:<4} {r['title']}  ->  {hint}")
    print(f"\nRecent events (now {datetime.now(timezone.utc).isoformat(timespec='seconds')}):")
    for e in db.recent_events(n_events):
        print(f"  {e['created_at']}  #{e['video_id'] or '-':<4} {e['event']:<22} {e['detail'] or ''}")
