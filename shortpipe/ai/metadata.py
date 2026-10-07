"""AI generation of YouTube titles, descriptions and tags, with YouTube limits enforced."""

from __future__ import annotations

import json
import re

from .client import AIClient

SYSTEM_PROMPT = """You write YouTube Shorts metadata for an educational channel.
Write an accurate, non-clickbait title (max 90 characters), a description of 2-4 short
sentences that explains what the viewer will learn, and 5-15 relevant lowercase tags.
Do not invent facts that are not supported by the provided metadata. Do not include URLs."""

METADATA_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "description", "tags"],
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
}

# YouTube Data API limits
MAX_TITLE = 100
MAX_DESCRIPTION = 5000
MAX_TAGS_CHARS = 500


def sanitize(meta: dict, attribution: str = "", ai_disclosure: bool = True) -> dict:
    # YouTube rejects '<' and '>' in titles and descriptions.
    title = re.sub(r"[<>]", "", meta["title"]).strip()[:MAX_TITLE] or "Untitled short"
    description = re.sub(r"[<>]", "", meta["description"]).strip()
    footer = []
    if attribution:
        footer.append(f"Credit: {attribution}")
    if ai_disclosure:
        footer.append("Title and description generated with AI assistance (university project).")
    if footer:
        description = f"{description}\n\n" + "\n".join(footer)
    description = description[:MAX_DESCRIPTION]

    tags, total = [], 0
    for tag in meta.get("tags", []):
        tag = re.sub(r"[<>,\"]", "", tag).strip().lstrip("#")
        if not tag or tag in tags:
            continue
        cost = len(tag) + (2 if " " in tag else 0) + (1 if tags else 0)  # quotes + comma
        if total + cost > MAX_TAGS_CHARS:
            break
        tags.append(tag)
        total += cost
    return {"title": title, "description": description, "tags": tags}


def generate_metadata(ai: AIClient, video) -> tuple[str, dict]:
    video = dict(video)
    prompt = json.dumps({
        "video": {
            "title": video["title"] or "",
            "description": video["description"] or "",
            "topic": video["topic"] or "",
            "hashtags": json.loads(video["hashtags"] or "[]"),
        }
    }, ensure_ascii=False)
    raw = ai.complete_json("youtube_metadata", SYSTEM_PROMPT, prompt, METADATA_SCHEMA)
    return prompt, sanitize(raw, attribution=video.get("attribution") or "")
