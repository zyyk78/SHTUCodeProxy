"""Regression coverage for the tool-result user-message compatibility switch."""
from config_store import AppConfig
from transformer import (
    anthropic_messages_to_chat_completions,
    responses_request_to_chat_completions,
)


def _anthropic_body():
    return {
        "model": "smoke-model",
        "messages": [
            {"role": "user", "content": "list files"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "call_1", "name": "list_files", "input": {}}
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "alpha.py\nbeta.py"},
                {"type": "text", "text": "Use this output."},
            ]},
        ],
        "tools": [{"name": "list_files", "input_schema": {"type": "object", "properties": {}}}],
    }


def _responses_body():
    return {
        "model": "smoke-model",
        "input": [
            {"type": "function_call", "call_id": "call_1", "name": "list_files", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "alpha.py\nbeta.py"},
        ],
    }


def test_anthropic_enabled_fallback_duplicates_tool_results():
    payload = anthropic_messages_to_chat_completions(
        _anthropic_body(), "fallback", "upstream", tool_result_visible_fallback=True
    )
    assert any(m.get("role") == "tool" and m.get("content") == "alpha.py\nbeta.py" for m in payload["messages"])
    assert any(m.get("role") == "user" and "<tool_results>" in m.get("content", "") for m in payload["messages"])


def test_anthropic_disabled_fallback_keeps_standard_tool_role_only():
    payload = anthropic_messages_to_chat_completions(
        _anthropic_body(), "fallback", "upstream", tool_result_visible_fallback=False
    )
    assert any(m.get("role") == "tool" and m.get("content") == "alpha.py\nbeta.py" for m in payload["messages"])
    assert not any("<tool_results>" in m.get("content", "") for m in payload["messages"])


def test_codex_enabled_fallback_duplicates_tool_results():
    payload = responses_request_to_chat_completions(
        _responses_body(), "fallback", "upstream", tool_result_visible_fallback=True
    )
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "call_1" for m in payload["messages"])
    assert any(m.get("role") == "user" and "<tool_results>" in m.get("content", "") for m in payload["messages"])


def test_codex_disabled_fallback_keeps_standard_tool_role_only():
    payload = responses_request_to_chat_completions(
        _responses_body(), "fallback", "upstream", tool_result_visible_fallback=False
    )
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "call_1" for m in payload["messages"])
    assert not any("<tool_results>" in m.get("content", "") for m in payload["messages"])


def test_config_switch_round_trip_defaults_to_previous_behavior():
    config = AppConfig.default()
    assert config.tool_result_visible_fallback is True
    restored = AppConfig.from_dict(config.to_dict() | {"tool_result_visible_fallback": "false"})
    assert restored.tool_result_visible_fallback is False


if __name__ == "__main__":
    fns = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for fn in fns:
        fn()
        print(f"OK: {fn.__name__}")
    print(f"all {len(fns)} passed")
