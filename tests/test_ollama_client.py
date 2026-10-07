"""OllamaClient against a fake server that speaks Ollama's /api/chat protocol."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from shortpipe.ai.analysis import ANALYSIS_SCHEMA, analyze_video
from shortpipe.ai.client import AIConfigError, AIError, OllamaClient

GOOD = {"topic": "math", "relevance": 0.8, "educational_value": 0.7,
        "content_flags": [], "recommend": True, "reasoning": "fits"}


class FakeOllama:
    def __init__(self):
        self.replies = []      # list of (status, body) served in order
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append((self.path, body))
                status, reply = fake.replies.pop(0)
                data = json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def chat_reply(self, content):
        self.replies.append((200, {"model": "m", "message": {"role": "assistant", "content": content},
                                   "done": True, "done_reason": "stop"}))


@pytest.fixture
def fake():
    f = FakeOllama()
    yield f
    f.server.shutdown()


def test_sends_schema_and_parses(fake):
    fake.chat_reply(json.dumps(GOOD))
    client = OllamaClient("llama3.2:3b", fake.url)
    assert client.complete_json("video_analysis", "sys", "{}", ANALYSIS_SCHEMA) == GOOD
    path, body = fake.requests[0]
    assert path == "/api/chat"
    assert body["model"] == "llama3.2:3b" and body["stream"] is False
    assert body["format"] == ANALYSIS_SCHEMA
    assert body["messages"][0] == {"role": "system", "content": "sys"}


def test_retries_bad_output_then_succeeds(fake):
    fake.chat_reply("not json")
    fake.chat_reply(json.dumps({"topic": "math"}))   # missing fields
    fake.chat_reply(json.dumps(GOOD))
    assert OllamaClient("m", fake.url).complete_json("t", "s", "{}", ANALYSIS_SCHEMA) == GOOD
    assert len(fake.requests) == 3


def test_gives_up_after_max_retries(fake):
    for _ in range(2):
        fake.chat_reply(json.dumps({"topic": 5}))
    with pytest.raises(AIError, match="unusable"):
        OllamaClient("m", fake.url, max_retries=2).complete_json("t", "s", "{}", ANALYSIS_SCHEMA)


def test_missing_model_is_config_error(fake):
    fake.replies.append((404, {"error": "model 'llama3.2:3b' not found"}))
    with pytest.raises(AIConfigError, match="ollama pull llama3.2:3b"):
        OllamaClient("llama3.2:3b", fake.url).complete_json("t", "s", "{}", ANALYSIS_SCHEMA)


def test_server_not_running_is_config_error():
    with pytest.raises(AIConfigError, match="ollama serve"):
        OllamaClient("m", "127.0.0.1:1").complete_json("t", "s", "{}", ANALYSIS_SCHEMA)


def test_scores_on_ten_point_scale_are_normalized(fake):
    fake.chat_reply(json.dumps({**GOOD, "relevance": 8, "educational_value": 65}))
    video = {"title": "t", "description": "d", "hashtags": "[]", "duration_seconds": 5,
             "views": 1, "likes": 0, "comments": 0, "shares": 0}
    _, result = analyze_video(OllamaClient("m", fake.url), video, ["math"], 0.5)
    assert result["relevance"] == 0.8 and result["educational_value"] == 0.65
