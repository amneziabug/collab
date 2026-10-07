"""AI providers that return schema-validated JSON: a local model via Ollama, the Claude
API (Anthropic SDK), and an offline heuristic provider for tests and demos."""

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


def check_schema(data: Any, schema: dict) -> str | None:
    """Minimal check of a flat object schema. Returns an error message or None.
    Local models sometimes ignore parts of the schema, so we verify before using it."""
    if not isinstance(data, dict):
        return "response is not a JSON object"
    types = {"string": str, "boolean": bool, "number": (int, float), "array": list, "object": dict}
    for key in schema.get("required", []):
        if key not in data:
            return f"missing field {key!r}"
        expected = schema["properties"][key]["type"]
        value = data[key]
        if expected == "number" and isinstance(value, bool):
            return f"field {key!r} should be a number"
        if not isinstance(value, types[expected]):
            return f"field {key!r} should be {expected}"
    return None


class OllamaClient(AIClient):
    """Local model served by Ollama (https://ollama.com) via its REST API.
    Uses Ollama's structured outputs: the JSON schema is passed as `format`."""

    def __init__(self, model: str, base_url: str = "http://localhost:11434",
                 timeout: int = 300, max_retries: int = 3):
        if "://" not in base_url:  # OLLAMA_HOST is often given as host:port
            base_url = f"http://{base_url}"
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries

    def _post(self, path: str, body: dict) -> dict:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            f"{self.base_url}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            try:
                detail = json.loads(detail).get("error", detail)
            except ValueError:
                pass
            if exc.code == 404:
                raise AIConfigError(f"Ollama: {detail}. Download it first: `ollama pull {self.model}`") from exc
            raise AIError(f"Ollama HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise AIError(f"Ollama did not answer within {self.timeout}s") from exc
            raise AIConfigError(f"Cannot reach Ollama at {self.base_url} ({exc.reason}). "
                                "Is it running? Start it with `ollama serve`.") from exc
        except TimeoutError as exc:
            raise AIError(f"Ollama did not answer within {self.timeout}s") from exc

    def describe_images(self, prompt: str, images_b64: list[str]) -> str:
        """Plain-text answer from a vision model (e.g. gemma3:4b) about the given images."""
        reply = self._post("/api/chat", {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt, "images": images_b64}],
            "stream": False,
            "options": {"temperature": 0.2},
        })
        text = (reply.get("message", {}).get("content") or "").strip()
        if not text:
            raise AIError(f"vision model {self.model} returned no description")
        return text

    def complete_json(self, task: str, system: str, user: str, schema: dict) -> dict[str, Any]:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "format": schema,
            "stream": False,
            "options": {"temperature": 0.2},
        }
        problem = "no attempts made"
        for attempt in range(1, self.max_retries + 1):
            reply = self._post("/api/chat", body)
            text = reply.get("message", {}).get("content", "")
            try:
                data = json.loads(text)
                problem = check_schema(data, schema)
            except json.JSONDecodeError as exc:
                problem = f"invalid JSON ({exc})"
            if problem is None:
                return data
            log.warning("Ollama %s output rejected (attempt %d/%d): %s",
                        task, attempt, self.max_retries, problem)
        raise AIError(f"Ollama gave unusable output for {task!r}: {problem}")


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
    if cfg.ai.provider == "ollama":
        return OllamaClient(cfg.ai.ollama_model, cfg.ai.ollama_url, cfg.ai.ollama_timeout,
                            cfg.ai.max_retries)
    if cfg.ai.provider == "claude":
        return ClaudeClient(cfg.ai.model, cfg.ai.effort, cfg.ai.max_retries)
    raise AIConfigError(f"unknown ai.provider {cfg.ai.provider!r} (use 'ollama', 'claude' or 'offline')")
