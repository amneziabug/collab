"""Generate a synthetic, fully self-owned test dataset (ffmpeg test patterns)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

DEMO_VIDEOS = [
    ("demo-001", "Why the sky is blue in 30 seconds", "Rayleigh scattering explained simply.",
     ["science", "physics"], (52000, 6100, 310, 420), "owned"),
    ("demo-002", "Python list comprehensions explained", "Turn a for loop into one line of programming.",
     ["programming", "python"], (18000, 1500, 95, 60), "owned"),
    ("demo-003", "The Pomodoro study tips that work", "A quick study tips routine for exam season.",
     ["study tips", "productivity"], (9100, 700, 50, 30), "cc0"),
    ("demo-004", "Pythagoras visual proof", "A visual math proof of a^2 + b^2 = c^2.",
     ["math", "geometry"], (31000, 2900, 140, 210), "cc-by"),
    ("demo-005", "My cat sleeping", "Just a cat.", ["cat"], (90000, 4000, 100, 50), "owned"),
    ("demo-006", "Binary search in one minute", "Programming interview classic, explained.",
     ["programming", "algorithms"], (400, 30, 2, 1), "owned"),
    ("demo-007", "Unknown origin clip about black holes", "Science clip found online.",
     ["science"], (250000, 20000, 900, 700), ""),
]


def make_demo_dataset(out_dir: Path, seconds: int = 5) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    videos = []
    for i, (vid, title, desc, tags, stats, license_) in enumerate(DEMO_VIDEOS):
        path = out_dir / f"{vid}.mp4"
        if not path.exists():
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error",
                 "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={seconds}",
                 "-f", "lavfi", "-i", f"sine=frequency={300 + 80 * i}:duration={seconds}",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
                check=True,
            )
        views, likes, comments, shares = stats
        videos.append({
            "id": vid, "file": path.name, "title": title, "description": desc,
            "creator": "Demo Student", "license": license_,
            "rights_holder": "Demo Student" if license_ else "",
            "attribution": "Demo Student, CC BY 4.0" if license_ == "cc-by" else "",
            "stats": {"views": views, "likes": likes, "comments": comments, "shares": shares},
            "hashtags": tags,
        })
    manifest = out_dir / "manifest.json"
    manifest.write_text(json.dumps({"videos": videos}, indent=2), encoding="utf-8")
    return manifest
