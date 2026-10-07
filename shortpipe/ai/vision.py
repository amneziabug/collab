"""Optional "watch the video" step: grab a few frames with ffmpeg and ask a local
vision model (via Ollama) what they show. The text summary then feeds the analysis
and metadata prompts, so titles describe what is actually on screen."""

from __future__ import annotations

import base64
import logging
import subprocess
from pathlib import Path

from .client import AIError, OllamaClient

log = logging.getLogger(__name__)

VISION_PROMPT = """These are {n} frames taken from one short video, in order.
Describe what the video shows in 2-4 plain sentences: the subject, any visible text,
what happens, and the setting. Only describe what you can see; do not guess a title."""


def extract_frames(path: Path, duration: float | None, count: int = 3, width: int = 512) -> list[bytes]:
    """JPEG frames spread evenly through the video."""
    duration = duration or 1.0
    frames = []
    for i in range(count):
        at = duration * (i + 1) / (count + 1)
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{at:.2f}", "-i", str(path), "-frames:v", "1",
             "-vf", f"scale={width}:-2", "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
            capture_output=True, timeout=60,
        )
        if result.returncode == 0 and result.stdout:
            frames.append(result.stdout)
    return frames


def describe_video(client: OllamaClient, path: Path, duration: float | None,
                   frame_count: int = 3) -> str:
    frames = extract_frames(path, duration, frame_count)
    if not frames:
        raise AIError(f"could not extract frames from {path}")
    images = [base64.b64encode(f).decode() for f in frames]
    return client.describe_images(VISION_PROMPT.format(n=len(images)), images)
