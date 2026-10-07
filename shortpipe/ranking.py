"""Scoring: combines engagement metrics with the AI relevance assessment."""

from __future__ import annotations

import math

WEIGHTS = {"engagement": 0.35, "reach": 0.15, "relevance": 0.35, "educational": 0.15}


def engagement_rate(views: int, likes: int, comments: int, shares: int) -> float:
    if views <= 0:
        return 0.0
    # Comments and shares signal stronger interest than likes.
    return (likes + 2 * comments + 3 * shares) / views


def engagement_score(views: int, likes: int, comments: int, shares: int) -> float:
    """0..1. An engagement rate of ~20% maps to ~1."""
    return min(1.0, engagement_rate(views, likes, comments, shares) / 0.20)


def reach_score(views: int) -> float:
    """0..1 on a log scale; 10M views ~ 1."""
    return min(1.0, math.log10(max(views, 1)) / 7)


def final_score(video: dict, relevance: float, educational: float) -> tuple[float, float]:
    eng = engagement_score(video["views"], video["likes"], video["comments"], video["shares"])
    score = (
        WEIGHTS["engagement"] * eng
        + WEIGHTS["reach"] * reach_score(video["views"])
        + WEIGHTS["relevance"] * relevance
        + WEIGHTS["educational"] * educational
    )
    return round(eng, 4), round(score, 4)
