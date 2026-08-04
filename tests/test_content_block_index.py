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


class _LineUpstream:
    """Upstream stub that reads byte-buffer line-by-line (real socket semantics)."""

    def __init__(self, chunks):
        self._buf = b"".join(chunks)

    def readline(self):
        if not self._buf:
            return b""
        i = self._buf.find(b"\n")
        if i < 0:
            line, self._buf = self._buf, b""
            return line
        line, self._buf = self._buf[: i + 1], self._buf[i + 1 :]
        return line

    def read(self, n=-1):
        return b""

    headers = property(lambda self: {"content-type": "text/event-stream"})
    status = property(lambda self: 200)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _jline(d):
    return (f"data: {json.dumps(d, ensure_ascii=False)}\n\n").encode("utf-8")


def test_reasoning_content_overlap(monkeypatch):
    """GLM transition delta carries reasoning tail + content head together.

    Regression: extract_text_delta returned only "reasoning" for a delta that
    had both reasoning_content and content, dropping the content (the first
    sentence after thinking). Assert the content survives as a text_delta.
    """
    chunks = [
        _jline({"choices": [{"delta": {"reasoning_content": "thinking..."}}]}),
        # transition delta: reasoning tail + content head in ONE delta
        _jline({"choices": [{"delta": {"reasoning_content": ".", "content": "你要的逻辑了。看 crop_resize"}}]}),
        _jline({"choices": [{"delta": {"content": ",即所有 P/V/mask"}}]}),
        _jline({"choices": [{"finish_reason": "stop", "delta": {}}]}),
        b"data: [DONE]\n\n",
    ]
    import proxy as proxy_mod

    monkeypatch.setattr(proxy_mod, "open_upstream", lambda *a, **kw: _LineUpstream(chunks))

    captured = io.BytesIO()
    handler = ProxyHandler.__new__(ProxyHandler)
    handler.wfile = captured
    handler.close_connection = False
    for m in ("send_response", "send_header", "end_headers", "flush_headers"):
        setattr(handler, m, lambda *a, **kw: None)
    body = {
        "model": "glm-chat",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "max_tokens": 1024,
        "_thinking_requested": True,
    }
    from config_store import ModelConfig

    model_config = ModelConfig(
        name="glm-chat", model_id="glm-chat", base_url="http://fake/v1/chat/completions", api_key="k",
        upstream_model="glm-chat", api_format="chat_completions", enable_thinking=True,
    )
    handler.handle_streaming(body, body, "tok", "http://fake/v1/chat/completions", 30, model_config)
    sse = captured.getvalue().decode("utf-8")

    text_parts = []
    for line in sse.split("\n"):
        if not line.startswith("data: "):
            continue
        try:
            evt = json.loads(line[len("data: "):])
        except Exception:
            continue
        if evt.get("type") == "content_block_delta" and evt.get("delta", {}).get("type") == "text_delta":
            text_parts.append(evt["delta"]["text"])
    joined = "".join(text_parts)
    assert "你要的逻辑了" in joined, f"content head lost in transition delta: {joined!r}"
    assert "crop_resize" in joined, f"content head incomplete: {joined!r}"
    # block index consistency still holds
    inv = _index_inventory(sse)
    for idx, counts in inv.items():
        assert counts["starts"] >= 1, f"index {idx} has stop/delta but no start: {counts}"
        assert counts["stops"] <= counts["starts"], f"index {idx} has more stops than starts: {counts}"


def test_reasoning_only_no_duplicate_text(monkeypatch):
    """Upstream returns ONLY reasoning_content (no content) with thinking requested.

    Regression: handle_streaming's tail fallback duplicated reasoning — emitted it
    once as a thinking block (collapsible) then again as a text_delta (answer),
    so the user saw the same text in both places. Assert reasoning appears only
    inside thinking_delta, never as text_delta, and an empty text block still
    exists so Claude Code reactive compact sees an assistant message.
    """
    chunks = [
        _jline({"choices": [{"delta": {"reasoning_content": "Let me compute 2+2. 2+2=4."}}]}),
        _jline({"choices": [{"delta": {"reasoning_content": " The answer is 4."}}]}),
        _jline({"choices": [{"finish_reason": "stop", "delta": {}}]}),
        b"data: [DONE]\n\n",
    ]
    import proxy as proxy_mod

    monkeypatch.setattr(proxy_mod, "open_upstream", lambda *a, **kw: _LineUpstream(chunks))

    captured = io.BytesIO()
    handler = ProxyHandler.__new__(ProxyHandler)
    handler.wfile = captured
    handler.close_connection = False
    for m in ("send_response", "send_header", "end_headers", "flush_headers"):
        setattr(handler, m, lambda *a, **kw: None)
    body = {
        "model": "glm-chat",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "max_tokens": 1024,
        "_thinking_requested": True,
    }
    from config_store import ModelConfig

    model_config = ModelConfig(
        name="glm-chat", model_id="glm-chat", base_url="http://fake/v1/chat/completions", api_key="k",
        upstream_model="glm-chat", api_format="chat_completions", enable_thinking=True,
    )
    handler.handle_streaming(body, body, "tok", "http://fake/v1/chat/completions", 30, model_config)
    sse = captured.getvalue().decode("utf-8")

    thinking_parts, text_parts = [], []
    text_block_exists = False
    for line in sse.split("\n"):
        if not line.startswith("data: "):
            continue
        try:
            evt = json.loads(line[len("data: "):])
        except Exception:
            continue
        if evt.get("type") == "content_block_start" and evt.get("content_block", {}).get("type") == "text":
            text_block_exists = True
        if evt.get("type") != "content_block_delta":
            continue
        dtype = evt.get("delta", {}).get("type")
        if dtype == "thinking_delta":
            thinking_parts.append(evt["delta"]["thinking"])
        elif dtype == "text_delta":
            text_parts.append(evt["delta"]["text"])
    thinking_joined = "".join(thinking_parts)
    text_joined = "".join(text_parts)

    # reasoning fully captured in the thinking block
    assert "2+2=4" in thinking_joined, f"reasoning missing from thinking block: {thinking_joined!r}"
    # and NOT duplicated into the text block
    assert "2+2=4" not in text_joined, f"reasoning leaked into text block (duplicate): {text_joined!r}"
    assert "The answer is 4" not in text_joined, f"reasoning leaked into text block (duplicate): {text_joined!r}"
    # a text block still exists (empty) so Claude Code compact sees an assistant message
    assert text_block_exists, "expected at least one text content_block for compact"
    # block index consistency still holds
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
    test_reasoning_content_overlap(mp)
    test_reasoning_only_no_duplicate_text(mp)
    print("OK: all thinking/streaming regression tests consistent")