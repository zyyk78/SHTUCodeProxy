"""Regression coverage for Anthropic recovery-path fixes.

Bug 1: the non-stream recovery path turned reasoning into literal
"🤔 Thinking" plain text (Claude Code render crash) because the converted
payload never carried the `_thinking_requested` flag.
Bug 1b: text blocks were sent with the full body in content_block_start *and*
again as a text_delta, so delta-accumulating SDKs duplicated the message.
Bug 2: reasoning-only streamed answers (glm-chat enable_thinking) triggered a
wasted non-stream re-request on every turn.
"""
import json

from transformer import (
    chat_completion_json_to_responses,
    responses_json_to_anthropic_message,
    thinking_requested,
)


class ModelConfig:
    model_id = "glm-chat"


def _convert(payload, think):
    converted = chat_completion_json_to_responses(payload, "glm-chat", 10, None, think)
    return converted


class TestThinkingFlagPropagation:
    def test_converted_payload_stamps_thinking_requested(self):
        converted = _convert({"choices": [{"message": {"reasoning_content": "r", "content": "a"}}]}, True)
        assert thinking_requested(converted) is True

    def test_converted_payload_does_not_stamp_when_not_requested(self):
        converted = _convert({"choices": [{"message": {"reasoning_content": "r", "content": "a"}}]}, False)
        assert thinking_requested(converted) is False

    def test_recovered_message_routes_reasoning_into_thinking_block(self):
        # Mirror the HTTPError recovery path: convert then build an Anthropic message.
        converted = _convert({"choices": [{"message": {"reasoning_content": "hidden reasoning", "content": "visible answer"}}]}, True)
        msg = responses_json_to_anthropic_message(converted, ModelConfig())
        types = [b["type"] for b in msg["content"]]
        assert types == ["thinking", "text"], types
        assert msg["content"][0]["thinking"] == "hidden reasoning"
        assert msg["content"][1]["text"] == "visible answer"
        assert "🤔 Thinking" not in json.dumps(msg)

    def test_recovered_message_does_not_leak_literal_thinking_text(self):
        # The pre-fix crash signature: reasoning stuffed into a single text block.
        converted = _convert({"choices": [{"message": {"reasoning_content": "hidden reasoning", "content": "visible answer"}}]}, True)
        msg = responses_json_to_anthropic_message(converted, ModelConfig())
        for block in msg["content"]:
            if block["type"] == "text":
                assert block["text"] == "visible answer"


class TestRecoveredStreamEmission:
    def _run(self, anthropic_msg):
        import proxy

        events = []

        class Fake(proxy.ProxyHandler):
            def __init__(self):  # bypass BaseHTTPRequestHandler init
                pass

            def _write_sse(self, event, payload):
                events.append((event, payload))

        handler = Fake.__new__(Fake)
        # write_sse is a module-level function taking self; bind a stub writer.
        import proxy as proxy_mod

        orig = proxy_mod.write_sse
        try:
            proxy_mod.write_sse = lambda self, event, payload: events.append((event, payload))
            proxy_mod.ProxyHandler._emit_anthropic_message_as_stream(handler, anthropic_msg, 0)
        finally:
            proxy_mod.write_sse = orig
        return events

    def test_text_block_start_is_empty_and_body_only_in_delta(self):
        msg = {"content": [{"type": "text", "text": "hello world"}], "usage": {}, "stop_reason": "end_turn"}
        events = self._run(msg)
        starts = [p for e, p in events if e == "content_block_start"]
        deltas = [p for e, p in events if e == "content_block_delta"]
        assert starts[0]["content_block"] == {"type": "text", "text": ""}
        assert deltas[0]["delta"] == {"type": "text_delta", "text": "hello world"}

    def test_thinking_block_start_is_empty_and_body_in_delta(self):
        msg = {"content": [{"type": "thinking", "thinking": "deep thought"}], "usage": {}, "stop_reason": "end_turn"}
        events = self._run(msg)
        starts = [p for e, p in events if e == "content_block_start"]
        deltas = [p for e, p in events if e == "content_block_delta"]
        assert starts[0]["content_block"] == {"type": "thinking", "thinking": ""}
        assert deltas[0]["delta"] == {"type": "thinking_delta", "thinking": "deep thought"}


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if isinstance(v, type) and k.startswith("Test")]
    count = 0
    for cls in fns:
        inst = cls()
        for name in sorted(dir(inst)):
            if name.startswith("test_"):
                getattr(inst, name)()
                print(f"OK: {cls.__name__}.{name}")
                count += 1
    print(f"all {count} passed")
