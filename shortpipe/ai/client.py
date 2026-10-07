"""Thin wrapper around the OpenAI Responses API returning schema-validated JSON,
plus an offline heuristic provider so the pipeline runs without an API key."""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

log = logging.getLogger(__name__)


class AIError(RuntimeError):
    pass


class AIClient(ABC):
    model: str

    @abstractmethod
    def complete_json(self, task: str, system: str, user: str, schema: dict) -> dict[str, Any]:
        """Return a dict that conforms to `schema`. `task` names the call (for offline mode)."""


class OpenAIClient(AIClient):
    def __init__(self, api_key: str, model: str, temperature: float = 0.4, max_retries: int = 3):
        from openai import OpenAI  # imported lazily so offline mode needs no SDK

        # The SDK retries connection errors/429/5xx itself; we add retries for bad JSON.
        self._client = OpenAI(api_key=api_key, max_retries=max_retries, timeout=60)
        self.model = model
        self.temperature = temperature
        self.max_retries = max_retries

    def complete_json(self, task: str, system: str, user: str, schema: dict) -> dict[str, Any]:
        from openai import APIError

        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._client.responses.create(
                    model=self.model,
                    instructions=system,
                    input=user,
                    temperature=self.temperature,
                    text={"format": {"type": "json_schema", "name": task,
                                     "schema": schema, "strict": True}},
                )
                return json.loads(response.output_text)
            except json.JSONDecodeError as exc:
                last_exc = exc
                log.warning("AI returned invalid JSON (attempt %d/%d)", attempt, self.max_retries)
            except APIError as exc:
                last_exc = exc
                log.warning("OpenAI API error (attempt %d/%d): %s", attempt, self.max_retries, exc)
            time.sleep(2 ** attempt)
        raise AIError(f"AI call {task!r} failed after {self.max_retries} attempts: {last_exc}")


class OfflineClient(AIClient):
    """Deterministic keyword heuristics. Useful for tests, CI and demos without a key."""

    model = "offline-heuristic"

    def complete_json(self, task: str, system: str, user: str, schema: dict) -> dict[str, Any]:
        payload = json.loads(user)
        if task == "video_analysis":
            return self._analyze(payload)
        if task == "youtube_metadata":
            return self._metadata(payload)
        raise AIError(f"offline client has no handler for task {task!r}")

    @staticmethod
    def _analyze(p: dict) -> dict:
        text = " ".join([p["video"]["title"], p["video"]["description"], *p["video"]["hashtags"]]).lower()
        matches = [t for t in p["criteria"]["topics"] if t.lower() in text]
        relevance = min(1.0, 0.3 + 0.35 * len(matches)) if matches else 0.2
        return {
            "topic": matches[0] if matches else "other",
            "relevance": round(relevance, 2),
            "educational_value": round(relevance, 2),
            "content_flags": [],
            "recommend": relevance >= p["criteria"]["min_relevance"],
            "reasoning": f"Keyword matches: {matches or 'none'}",
        }

    @staticmethod
    def _metadata(p: dict) -> dict:
        v = p["video"]
        title = v["title"][:90].strip() or "Educational short"
        tags = list(dict.fromkeys([v.get("topic", ""), *v["hashtags"], "education", "shorts"]))
        return {
            "title": f"{title} #shorts"[:100],
            "description": f"{v['description']}\n\n#shorts #{v.get('topic', 'learning').replace(' ', '')}",
            "tags": [t for t in tags if t][:15],
        }


def make_ai_client(cfg) -> AIClient:
    if cfg.ai.provider == "offline":
        return OfflineClient()
    if cfg.ai.provider != "openai":
        raise ValueError(f"unknown ai.provider {cfg.ai.provider!r}")
    if not cfg.openai_api_key:
        raise AIError("OPENAI_API_KEY is not set (or use ai.provider = 'offline')")
    return OpenAIClient(cfg.openai_api_key, cfg.ai.model, cfg.ai.temperature, cfg.ai.max_retries)
