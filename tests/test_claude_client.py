"""ClaudeClient request shape and response handling, with the SDK call mocked out."""

import json
from types import SimpleNamespace

import pytest

from shortpipe.ai.analysis import ANALYSIS_SCHEMA
from shortpipe.ai.client import AIConfigError, AIError, ClaudeClient


def _response(text=None, stop_reason="end_turn", category=None):
    content = [SimpleNamespace(type="text", text=text)] if text is not None else []
    return SimpleNamespace(content=content, stop_reason=stop_reason,
                           stop_details=SimpleNamespace(category=category))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    c = ClaudeClient("claude-opus-5-5", effort="low")
    calls = []

    def install(result):
        def create(**kwargs):
            calls.append(kwargs)
            if isinstance(result, Exception):
                raise result
            return result
        monkeypatch.setattr(c._client.beta.messages, "create", create)
        return calls
    c.install = install
    return c


def test_sends_structured_output_request(client):
    calls = client.install(_response(json.dumps({"topic": "math"})))
    assert client.complete_json("video_analysis", "sys", "{}", ANALYSIS_SCHEMA) == {"topic": "math"}
    req = calls[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["system"] == "sys"
    assert req["output_config"] == {"effort": "low",
                                    "format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA}}
    assert req["fallbacks"] == "default" and req["betas"] == [ClaudeClient.FALLBACK_BETA]
    assert "temperature" not in req and "thinking" not in req


def test_refusal_and_truncation_raise(client):
    client.install(_response(stop_reason="refusal", category="cyber"))
    with pytest.raises(AIError, match="declined"):
        client.complete_json("t", "s", "{}", {})
    client.install(_response("{", stop_reason="max_tokens"))
    with pytest.raises(AIError, match="max_tokens"):
        client.complete_json("t", "s", "{}", {})


def test_missing_credentials_is_config_error(client):
    client.install(TypeError("Could not resolve authentication method. Expected one of api_key"))
    with pytest.raises(AIConfigError):
        client.complete_json("t", "s", "{}", {})
