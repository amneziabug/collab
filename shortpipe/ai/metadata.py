"""AI generation of YouTube titles, descriptions and tags, with YouTube limits enforced."""

from __future__ import annotations

import json
import re

from .client import AIClient

SYSTEM_PROMPT = """You write YouTube Shorts metadata for an educational channel.
Write an accurate, non-clickbait title (max 90 characters), a description of 2-4 short
sentences that explains what the viewer will learn, and 5-15 relevant lowercase tags.
Do not invent facts that are not supported by the provided metadata. Do not include URLs.
The original title may just be a filename; when `visual_summary` is given, base the title and
description on what it says the video shows."""

CREATOR_PROMPT = """You write YouTube Shorts metadata for a creator's own short videos.
Title: short and specific (max 60 characters), says what actually happens in the video and
makes people curious. No hashtags in the title, at most one emoji, nothing misleading.
Description: 1-2 short sentences, then 2-3 relevant hashtags on their own line.
Tags: 5-10 relevant lowercase keywords.
Base everything on `visual_summary` when it is given; the original title may just be a
meaningless filename (e.g. "Snaptik 7671934543603059990 v3"), so ignore such names.
Do not invent facts that are not supported by the provided information. Do not include URLs."""

STYLES = {"educational": SYSTEM_PROMPT, "creator": CREATOR_PROMPT}

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
        footer.append("Title and description written with AI assistance.")
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


def generate_metadata(ai: AIClient, video, style: str = "educational",
                      ai_note: bool = True) -> tuple[str, dict]:
    video = dict(video)
    prompt = json.dumps({
        "video": {
            "title": video["title"] or "",
            "description": video["description"] or "",
            "topic": video["topic"] or "",
            "hashtags": json.loads(video["hashtags"] or "[]"),
            "visual_summary": video.get("visual_summary") or "",
        }
    }, ensure_ascii=False)
    raw = ai.complete_json("youtube_metadata", STYLES[style], prompt, METADATA_SCHEMA)
    return prompt, sanitize(raw, attribution=video.get("attribution") or "", ai_disclosure=ai_note)
