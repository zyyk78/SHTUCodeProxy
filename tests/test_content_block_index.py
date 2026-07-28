"""Minimal regression test for content_block index collision.

Reproduces the bug fixed in proxy.py:handle_streaming where reasoning
interleaving with text caused content_block index collisions and stray
stop events on unseen indices, triggering Claude Code's
"Content block not found" error.

Strategy: import proxy.handle_streaming and feed synthetic SSE events
in two orderings (reasoning-first, text-first) and assert that every
content_block_stop event references a content_block_start event with
the same index.
"""
import io
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from proxy import ProxyHandler  # noqa: E402


class _FakeUpstream:
    """Yields predetermined SSE chunks, simulating a chat-completions upstream."""

    def __init__(self, chunks):
        self._chunks = chunks
        self._pos = 0

    def readline(self):
        if self._pos >= len(self._chunks):
            return b""
        line = self._chunks[self._pos]
        self._pos += 1
        return line

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _install_fake_upstream(monkeypatch, chunks):
    import proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "open_upstream", lambda *a, **kw: _FakeUpstream(chunks))


def _run_streaming(events_order, monkeypatch):
    """events_order: list of ('reasoning'|'text', text) tuples to feed upstream."""
    chunks = []
    for kind, text in events_order:
        if kind == "reasoning":
            chunks.append(
                f"data: {{\"choices\":[{{\"delta\":{{\"reasoning_content\":\"{text}\"}}}}]}}\n\n".encode()
            )
        else:
            chunks.append(
                f"data: {{\"choices\":[{{\"delta\":{{\"content\":\"{text}\"}}}}]}}\n\n".encode()
            )
    chunks.append(b"data: [DONE]\n\n")
    _install_fake_upstream(monkeypatch, chunks)

    import proxy as proxy_mod
    from config_store import ModelConfig

    captured = io.BytesIO()

    class Handler(ProxyHandler):
        # Stub out socket-level methods so we can drive the handler in-process.
        def setup(self):
            pass

    # Build a minimal handler instance bound to captured stream.
    handler = ProxyHandler.__new__(ProxyHandler)
    handler.wfile = captured
    handler.close_connection = False

    body = {
        "model": "glm-chat",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "max_tokens": 1024,
    }
    auth_token = "test-token"
    upstream_url = "http://fake/v1/chat/completions"
    timeout = 30
    model_config = ModelConfig(
        name="glm-chat", model_id="glm-chat", base_url=upstream_url, api_key="k",
        upstream_model="glm-chat",
        api_format="chat_completions",
    )
    handler.handle_streaming(body, body, auth_token, upstream_url, timeout, model_config)
    return captured.getvalue().decode("utf-8")


def _index_inventory(sse_text):
    """Return dict: index -> {'starts': N, 'stops': N, 'deltas': N} for content_block events."""
    inv = {}
    for line in sse_text.split("\n"):
        if not line.startswith("data: "):
            continue
        try:
            evt = json.loads(line[len("data: "):])
        except Exception:
            continue
        if evt.get("type") not in ("content_block_start", "content_block_stop", "content_block_delta"):
            continue
        idx = evt.get("index")
        if idx is None:
            continue
        slot = inv.setdefault(idx, {"starts": 0, "stops": 0, "deltas": 0})
        slot[evt["type"].split("_")[-1] + "s"] = slot.get(evt["type"].split("_")[-1] + "s", 0) + 1
    return inv


def test_text_then_reasoning(monkeypatch):
    """Upstream emits text delta BEFORE reasoning delta. Regression case."""
    body = {
        "model": "glm-chat",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "max_tokens": 1024,
    }
    chunks = [
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
        b'data: {"choices":[{"delta":{"reasoning_content":"thinking..."}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":" bye"}}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    import proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "open_upstream", lambda *a, **kw: _FakeUpstream(chunks))

    captured = io.BytesIO()
    handler = ProxyHandler.__new__(ProxyHandler)
    handler.wfile = captured
    handler.close_connection = False
    handler.send_response = lambda *a, **kw: None
    handler.send_header = lambda *a, **kw: None
    handler.end_headers = lambda *a, **kw: None
    handler.flush_headers = lambda *a, **kw: None
    from config_store import ModelConfig
    model_config = ModelConfig(
        name="glm-chat", model_id="glm-chat", base_url="http://fake/v1/chat/completions", api_key="k",
        upstream_model="glm-chat",
        api_format="chat_completions",
    )
    handler.handle_streaming(body, body, "tok", "http://fake/v1/chat/completions", 30, model_config)
    sse = captured.getvalue().decode("utf-8")
    inv = _index_inventory(sse)
    # Every index that has a stop must also have a start.
    for idx, counts in inv.items():
        assert counts["starts"] >= 1, f"index {idx} has stop/delta but no start: {counts}"
        assert counts["stops"] <= counts["starts"], f"index {idx} has more stops than starts: {counts}"


def test_reasoning_then_text(monkeypatch):
    chunks = [
        b'data: {"choices":[{"delta":{"reasoning_content":"thinking..."}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"answer"}}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    import proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "open_upstream", lambda *a, **kw: _FakeUpstream(chunks))

    captured = io.BytesIO()
    handler = ProxyHandler.__new__(ProxyHandler)
    handler.wfile = captured
    handler.close_connection = False
    handler.send_response = lambda *a, **kw: None
    handler.send_header = lambda *a, **kw: None
    handler.end_headers = lambda *a, **kw: None
    handler.flush_headers = lambda *a, **kw: None
    body = {
        "model": "glm-chat",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "max_tokens": 1024,
    }
    from config_store import ModelConfig
    model_config = ModelConfig(
        name="glm-chat", model_id="glm-chat", base_url="http://fake/v1/chat/completions", api_key="k",
        upstream_model="glm-chat",
        api_format="chat_completions",
    )
    handler.handle_streaming(body, body, "tok", "http://fake/v1/chat/completions", 30, model_config)
    sse = captured.getvalue().decode("utf-8")
    inv = _index_inventory(sse)
    for idx, counts in inv.items():
        assert counts["starts"] >= 1, f"index {idx} has stop/delta but no start: {counts}"
        assert counts["stops"] <= counts["starts"], f"index {idx} has more stops than starts: {counts}"


if __name__ == "__main__":
    # No pytest? Run monkeypatch via simple env-var stand-in.
    import contextlib

    @contextlib.contextmanager
    def _noop():
        yield

    class _MP:
        def setattr(self, *a, **kw):
            pass

    mp = _MP()
    test_text_then_reasoning(mp)
    test_reasoning_then_text(mp)
    print("OK: text_then_reasoning + reasoning_then_text both produce consistent indices")