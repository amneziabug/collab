"""Thin wrapper around the Claude API (Anthropic SDK) returning schema-validated JSON,
plus an offline heuristic provider so the pipeline runs without an API key."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any

log = logging.getLogger(__name__)


class AIError(RuntimeError):
    """A single AI call failed (the video is marked failed and can be retried)."""


class AIConfigError(AIError):
    """The AI provider can't be used at all (missing/invalid key, unknown model).
    Stages stop instead of marking every video failed."""


class AIClient(ABC):
    model: str

    @abstractmethod
    def complete_json(self, task: str, system: str, user: str, schema: dict) -> dict[str, Any]:
        """Return a dict that conforms to `schema`. `task` names the call (for offline mode)."""


class ClaudeClient(AIClient):
    # Server-side fallback: if the model declines a request, the API re-runs it on a
    # suitable fallback model inside the same call.
    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, model: str, effort: str = "medium", max_retries: int = 3,
                 max_tokens: int = 4096):
        import anthropic

        try:
            # Credentials come from ANTHROPIC_API_KEY (or an `ant auth login` profile).
            # The SDK retries connection errors, 408/409/429 and 5xx with backoff.
            self._client = anthropic.Anthropic(max_retries=max_retries, timeout=120)
        except anthropic.AnthropicError as exc:
            raise AIConfigError(f"Claude client setup failed: {exc}. Set ANTHROPIC_API_KEY "
                                "(or use ai.provider = 'offline')") from exc
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens

    def complete_json(self, task: str, system: str, user: str, schema: dict) -> dict[str, Any]:
        import anthropic

        try:
            response = self._client.beta.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": self.effort,
                               "format": {"type": "json_schema", "schema": schema}},
                betas=[self.FALLBACK_BETA],
                fallbacks="default",
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                anthropic.NotFoundError) as exc:
            raise AIConfigError(f"Claude API rejected the request ({exc.status_code}): {exc.message}") from exc
        except anthropic.BadRequestError as exc:
            raise AIError(f"Claude API bad request: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise AIError(f"Claude API error {exc.status_code} after retries: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise AIError(f"could not reach the Claude API after retries: {exc}") from exc
        except anthropic.AnthropicError as exc:
            raise AIConfigError(f"Claude client error: {exc}") from exc
        except TypeError as exc:
            # The SDK raises TypeError when no API key or login profile can be found.
            if "authentication" not in str(exc):
                raise
            raise AIConfigError("No Claude credentials found. Set ANTHROPIC_API_KEY "
                                "(or use ai.provider = 'offline')") from exc

        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None)
            raise AIError(f"Claude declined the {task!r} request (category: {category})")
        if response.stop_reason == "max_tokens":
            raise AIError(f"Claude output for {task!r} hit max_tokens={self.max_tokens}")

        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise AIError(f"Claude returned no text for {task!r} (stop_reason={response.stop_reason})")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise AIError(f"Claude returned invalid JSON for {task!r}: {exc}") from exc


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
    if cfg.ai.provider != "claude":
        raise AIConfigError(f"unknown ai.provider {cfg.ai.provider!r} (use 'claude' or 'offline')")
    return ClaudeClient(cfg.ai.model, cfg.ai.effort, cfg.ai.max_retries)
