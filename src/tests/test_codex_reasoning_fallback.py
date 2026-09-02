"""Regression coverage for Codex reasoning split and fallback state consistency."""
from transformer import (
    _codex_emit_recovered_response,
    chat_completion_json_to_responses,
)


def test_reasoning_with_content_is_split_when_explicit_thinking_requested():
    payload = {"choices": [{"message": {"reasoning_content": "hidden reasoning", "content": "visible answer"}}]}
    out = chat_completion_json_to_responses(payload, "glm-chat", 10, None, True)
    assert [item["type"] for item in out["output"]] == ["reasoning", "message"]
    assert out["output"][0]["summary"][0]["text"] == "hidden reasoning"
    assert out["output"][1]["content"][0]["text"] == "visible answer"


def test_reasoning_only_remains_hidden_when_thinking_requested():
    payload = {"choices": [{"message": {"reasoning_content": "hidden reasoning", "content": ""}}]}
    out = chat_completion_json_to_responses(payload, "glm-chat", 10, None, True)
    assert [item["type"] for item in out["output"]] == ["reasoning"]
    assert out["output"][0]["summary"][0]["text"] == "hidden reasoning"


def test_reasoning_only_is_visible_without_thinking_request():
    payload = {"choices": [{"message": {"reasoning_content": "model answer"}}]}
    out = chat_completion_json_to_responses(payload, "glm-chat", 10, None, False)
    assert [item["type"] for item in out["output"]] == ["message"]
    assert out["output"][0]["content"][0]["text"] == "model answer"


def test_recovered_response_emits_real_reasoning_and_distinct_indices():
    converted = {
        "id": "resp_test",
        "output": [
            {"id": "rs", "type": "reasoning", "status": "completed", "summary": [{"type": "summary_text", "text": "real reasoning"}]},
            {"id": "msg", "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]},
            {"id": "tool", "type": "function_call", "call_id": "call_1", "name": "shell", "arguments": "{}"},
        ],
        "usage": {"input_tokens": 3, "output_tokens": 4},
    }
    events = []

    def emit(kind, payload):
        events.append((kind, payload))

    class ModelConfig:
        model_id = "glm-chat"

    _codex_emit_recovered_response(emit, "resp_test", "msg", converted, ModelConfig(), True)

    added = [payload for kind, payload in events if kind == "response.output_item.added"]
    assert added[0]["item"]["type"] == "reasoning"
    assert added[0]["item"]["summary"][0]["text"] == "real reasoning"
    assert added[1]["item"]["type"] == "message"
    assert [payload["output_index"] for payload in added] == [0, 1, 2]
    assert len({payload["output_index"] for kind, payload in events if "output_index" in payload}) == 3
    assert [item["type"] for item in events[-1][1]["response"]["output"]] == ["reasoning", "message", "function_call"]


if __name__ == "__main__":
    fns = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for fn in fns:
        fn()
        print(f"OK: {fn.__name__}")
    print(f"all {len(fns)} passed")
