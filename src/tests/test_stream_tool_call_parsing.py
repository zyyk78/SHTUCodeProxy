"""流式工具调用解析的健壮性: 各种真实世界的上游形态, 有没有会被**静默丢弃**的。

背景: 代理日志里的 `tools=0` 无法区分“上游没发工具调用”与“上游发了但解析时丢了”。
这里把已知/可能的 chat_completions 流式工具调用形态逐个喂给 extract_text_delta(),
断言都能被识别成 tool_call* kind 而不是落到 "ignore" / "done"。
"""
from transformer import extract_text_delta, merge_tool_call_payloads

import json


def _sse(obj):
    return json.dumps(obj)


def _chunk(delta=None, message=None, finish_reason=None):
    choice = {}
    if delta is not None:
        choice["delta"] = delta
    if message is not None:
        choice["message"] = message
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {"choices": [choice]}


# ---------------------------------------------------- 形态 1: 标准增量 tool_calls


def test_standard_incremental_tool_call_deltas():
    frames = [
        _sse(_chunk(delta={"role": "assistant", "tool_calls": [
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "multi_agent_v1__close_agent", "arguments": ""}}]})),
        _sse(_chunk(delta={"tool_calls": [
            {"index": 0, "function": {"arguments": '{"target"'}}]})),
        _sse(_chunk(delta={"tool_calls": [
            {"index": 0, "function": {"arguments": ':"01a1"}'}}]})),
        _sse(_chunk(delta={}, finish_reason="tool_calls")),
    ]
    tool_calls = []
    kinds = []
    for frame in frames:
        kind, parsed = extract_text_delta(None, frame)
        kinds.append(kind)
        if kind in ("tool_call", "tool_call_delta", "tool_calls", "tool_calls_delta"):
            merge_tool_call_payloads(tool_calls, parsed)
    assert kinds[:3] == ["tool_call_delta"] * 3, kinds
    assert kinds[3] == "done"
    assert tool_calls[0]["name"] == "multi_agent_v1__close_agent"
    assert json.loads(tool_calls[0]["arguments"]) == {"target": "01a1"}


# --------------------------------- 形态 2: 工具调用只在最后一个 chunk 里给全量


def test_full_tool_call_in_final_chunk_with_empty_delta():
    """有些网关把完整 tool_calls 放在 finish_reason 那个 chunk 的 delta 里。"""
    frame = _sse(_chunk(
        delta={"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                               "function": {"name": "spawn_agent", "arguments": "{}"}}]},
        finish_reason="tool_calls"))
    tool_calls = []
    for data in (frame,):
        kind, parsed = extract_text_delta(None, data)
        assert kind in ("tool_call", "tool_call_delta", "tool_calls", "tool_calls_delta"), kind
        merge_tool_call_payloads(tool_calls, parsed)
    assert tool_calls[0]["name"] == "spawn_agent"


# ------------------------------- 形态 3: 旧版单数 delta.function_call (危险)


def test_legacy_singular_function_call_delta():
    """旧版 OpenAI 格式: delta.function_call (单数)。丢在这里 = tools=0。"""
    frame = _sse(_chunk(delta={"role": "assistant",
                               "function_call": {"name": "close_agent", "arguments": "{}"}}))
    kind, parsed = extract_text_delta(None, frame)
    assert kind in ("tool_call", "tool_call_delta", "tool_calls", "tool_calls_delta"), \
        f"legacy function_call 被静默丢弃, kind={kind}"
    tool_calls = []
    merge_tool_call_payloads(tool_calls, parsed)
    assert tool_calls[0]["name"] == "close_agent"


def test_legacy_singular_function_call_in_message():
    frame = _sse(_chunk(message={"function_call": {"name": "close_agent", "arguments": "{}"}}))
    kind, parsed = extract_text_delta(None, frame)
    assert kind in ("tool_call", "tool_calls"), f"legacy message.function_call 被丢弃, kind={kind}"


def test_non_stream_converter_keeps_legacy_function_call():
    """非流式转换同样不能丢 legacy 格式。"""
    from transformer import chat_completion_json_to_responses, reset_codex_tool_route_registry
    reset_codex_tool_route_registry()
    payload = {
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "",
                        "function_call": {"name": "exec_command", "arguments": '{"cmd":"ls"}'}},
            "finish_reason": "function_call",
        }]
    }
    out = chat_completion_json_to_responses(payload, "m", 10)
    calls = [i for i in out["output"] if i.get("type") == "function_call"]
    assert len(calls) == 1, f"非流式 legacy function_call 被丢弃: {out['output']}"
    assert calls[0]["name"] == "exec_command"
    assert calls[0]["arguments"] == '{"cmd":"ls"}'


# ------------------------- 形态 4: reasoning 与 tool_calls 出现在同一个 delta


def test_reasoning_and_tool_calls_in_same_delta():
    """GLM 思考结束的过渡 delta 可能同时带 reasoning_content 和 tool_calls。"""
    frame = _sse(_chunk(delta={
        "reasoning_content": "让我关掉它",
        "tool_calls": [{"index": 0, "id": "c9", "type": "function",
                        "function": {"name": "close_agent", "arguments": "{}"}}]}))
    kind, parsed = extract_text_delta(None, frame)
    assert kind in ("tool_call", "tool_call_delta", "tool_calls", "tool_calls_delta"), \
        f"工具调用被 reasoning 分支抢走, kind={kind}"
    tool_calls = []
    merge_tool_call_payloads(tool_calls, parsed)
    assert tool_calls[0]["name"] == "close_agent"


# ------------------------------- 形态 5: 多工具并行


def test_parallel_tool_calls_multiple_indexes():
    frames = [
        _sse(_chunk(delta={"tool_calls": [
            {"index": 0, "id": "a", "type": "function",
             "function": {"name": "exec_command", "arguments": '{"cmd":"ls"}'}}]})),
        _sse(_chunk(delta={"tool_calls": [
            {"index": 1, "id": "b", "type": "function",
             "function": {"name": "write_stdin", "arguments": '{"chars":"x"}'}}]})),
    ]
    tool_calls = []
    for frame in frames:
        kind, parsed = extract_text_delta(None, frame)
        merge_tool_call_payloads(tool_calls, parsed)
    assert [t["name"] for t in tool_calls] == ["exec_command", "write_stdin"]


# --------------------- 形态 6: 只带 arguments 的续片, 没有 name 也不能丢


def test_argument_continuation_fragment_is_merged_not_dropped():
    frames = [
        _sse(_chunk(delta={"tool_calls": [
            {"index": 0, "id": "c", "type": "function",
             "function": {"name": "spawn_agent", "arguments": '{"message"'}}]})),
        _sse(_chunk(delta={"tool_calls": [
            {"index": 0, "function": {"arguments": ':"hi"}'}}]})),
    ]
    tool_calls = []
    for frame in frames:
        kind, parsed = extract_text_delta(None, frame)
        assert kind != "ignore", f"参数续片被丢弃, kind={kind}"
        merge_tool_call_payloads(tool_calls, parsed)
    assert json.loads(tool_calls[0]["arguments"]) == {"message": "hi"}
