"""Video processing with ffmpeg: trim, scale/pad to 9:16, normalise audio."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


class ProcessingError(RuntimeError):
    pass


def require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise ProcessingError(
                f"{tool} not found on PATH. Install ffmpeg first: "
                "`sudo apt install ffmpeg` (Ubuntu/WSL), `brew install ffmpeg` (macOS), "
                "`winget install ffmpeg` (Windows)"
            )


def probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise ProcessingError(f"ffprobe failed for {path}: {result.stderr.strip()}")
    info = json.loads(result.stdout)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ProcessingError(f"{path} has no video stream")
    return {
        "duration": float(info["format"].get("duration", 0)),
        "width": int(video["width"]),
        "height": int(video["height"]),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def process_video(src: Path, out_dir: Path, *, max_duration: int, width: int, height: int,
                  bitrate: str, timeout: int = 600, name: str | None = None) -> tuple[Path, float]:
    """Transcode `src` into a YouTube-Shorts-friendly MP4. Returns (path, duration)."""
    require_ffmpeg()
    src = Path(src)
    if not src.is_file():
        raise ProcessingError(f"source file missing: {src}")
    info = probe(src)
    duration = min(info["duration"], float(max_duration))
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"{name or src.stem}_short.mp4"
    tmp = dst.with_suffix(".part.mp4")

    vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
          f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30")
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src)]
    if not info["has_audio"]:
        # Add a silent track so every output has the same stream layout.
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000", "-shortest"]
    cmd += [
        "-t", f"{duration:.2f}", "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-b:v", bitrate, "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
    ]
    if info["has_audio"]:
        cmd += ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
    cmd += ["-movflags", "+faststart", str(tmp)]

    log.debug("ffmpeg: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        tmp.unlink(missing_ok=True)
        raise ProcessingError(f"ffmpeg timed out after {timeout}s") from exc
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise ProcessingError(f"ffmpeg failed: {result.stderr.strip()[-500:]}")
    tmp.replace(dst)
    return dst, duration
