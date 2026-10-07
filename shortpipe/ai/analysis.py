"""AI content analysis: topic classification, relevance and suitability."""

from __future__ import annotations

import json

from .client import AIClient

SYSTEM_PROMPT = """You are a content analyst for a university project that curates short
educational videos. Given one video's metadata and the selection criteria, classify its topic,
rate how relevant it is to the requested topics (0-1) and its educational value (0-1), and flag
any problems (e.g. "misleading", "unsafe", "low_quality", "not_educational").
Set `recommend` to true when relevance is at least `min_relevance` and there are no flags.
Leave `content_flags` empty when there are no problems.
Base your judgement only on the provided metadata. Be concise in `reasoning`."""

ANALYSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["topic", "relevance", "educational_value", "content_flags", "recommend", "reasoning"],
    "properties": {
        "topic": {"type": "string"},
        "relevance": {"type": "number"},
        "educational_value": {"type": "number"},
        "content_flags": {"type": "array", "items": {"type": "string"}},
        "recommend": {"type": "boolean"},
        "reasoning": {"type": "string"},
    },
}


def build_prompt(video: dict, topics: list[str], min_relevance: float) -> str:
    return json.dumps({
        "criteria": {"topics": topics, "min_relevance": min_relevance},
        "video": {
            "title": video["title"] or "",
            "description": video["description"] or "",
            "hashtags": json.loads(video["hashtags"] or "[]"),
            "duration_seconds": video["duration_seconds"],
            "stats": {k: video[k] for k in ("views", "likes", "comments", "shares")},
        },
    }, ensure_ascii=False)


def analyze_video(ai: AIClient, video, topics: list[str], min_relevance: float) -> tuple[str, dict]:
    prompt = build_prompt(dict(video), topics, min_relevance)
    result = ai.complete_json("video_analysis", SYSTEM_PROMPT, prompt, ANALYSIS_SCHEMA)
    for key in ("relevance", "educational_value"):
        result[key] = normalize_score(result[key])
    return prompt, result


def normalize_score(value) -> float:
    """Clamp to 0..1. Small local models sometimes answer on a 0-10 or 0-100 scale."""
    value = float(value)
    if value > 1:
        value = value / 10 if value <= 10 else value / 100
    return max(0.0, min(1.0, value))
