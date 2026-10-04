"""空回合 (只有 reasoning, 无正文无工具) 的检测与重试回收工具调用。

背景 (线上问题):
部分模型 (实测 glm-chat) 在多步工具调用链的收尾步会概率性返回
``output=[{type: reasoning}]`` 这种空回合, 客户端表现为“模型说要做什么但工具没执行”。
代理本来就有一次“空流重试”, 但只回收文本、**把重试里返回的工具调用全丢了** ——
于是模型本来要发的 close_agent 被降级成一句文本。

参考 docs/glm-empty-turn-repro.md
"""
from config_store import ModelConfig
from transformer import (
    codex_function_call_item,
    merge_tool_call_payloads,
    responses_output_has_content,
    responses_output_tool_calls,
    responses_tools_to_chat_tools,
    reset_codex_tool_route_registry,
)


def _fn(name, description="desc"):
    return {"type": "function", "name": name, "description": description,
            "parameters": {"type": "object", "properties": {}, "required": []}, "strict": False}


def _reasoning_only():
    return [{"id": "rs_1", "type": "reasoning", "status": "completed",
             "summary": [{"type": "summary_text", "text": "We already said. Now close."}]}]


# ------------------------------------------------------------ 空回合检测


def test_reasoning_only_output_is_empty_turn():
    assert responses_output_has_content(_reasoning_only()) is False


def test_empty_list_is_empty_turn():
    assert responses_output_has_content([]) is False


def test_message_with_text_is_not_empty():
    out = [{"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "现在关闭它。"}]}]
    assert responses_output_has_content(out) is True


def test_message_with_blank_text_is_empty():
    out = [{"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "   "}]}]
    assert responses_output_has_content(out) is False


def test_function_call_is_not_empty():
    assert responses_output_has_content(
        [{"type": "function_call", "name": "close_agent"}]) is True


def test_custom_tool_call_is_not_empty():
    assert responses_output_has_content(
        [{"type": "custom_tool_call", "name": "apply_patch"}]) is True


# ------------------------------------------------- 重试时回收工具调用 (核心)


def test_retry_recovers_namespaced_tool_call_with_namespace_preserved():
    """重试里返回的 close_agent 必须带着 namespace 回到 codex, 不能退化成裸名。"""
    reset_codex_tool_route_registry()
    tools = [{
        "type": "namespace",
        "name": "multi_agent_v1",
        "tools": [_fn("close_agent")],
    }]
    responses_tools_to_chat_tools(tools)

    # 模拟 chat_completion_json_to_responses 的输出: 已解回 (name, namespace)
    converted_output = [{
        "id": "chatcmpl-tool-1", "type": "function_call", "status": "completed",
        "call_id": "chatcmpl-tool-1", "name": "close_agent",
        "namespace": "multi_agent_v1", "arguments": '{"target":"01a1"}',
    }]

    calls = responses_output_tool_calls(converted_output)
    assert len(calls) == 1
    # 名字必须还原成拍平名, 否则下游查路由表会丢 namespace
    assert calls[0]["name"] == "multi_agent_v1__close_agent"
    assert calls[0]["id"] == "chatcmpl-tool-1"

    # 走真实发射路径: merge -> codex_function_call_item
    merged: list = []
    for recovered in calls:
        merge_tool_call_payloads(merged, recovered)
    item = codex_function_call_item(merged[0], 0)
    assert item["name"] == "close_agent"
    assert item["namespace"] == "multi_agent_v1"
    assert item["call_id"] == "chatcmpl-tool-1"


def test_retry_recovers_multiple_tool_calls_without_overwriting():
    """index 必须递增, 否则多个工具调用会互相覆盖。"""
    reset_codex_tool_route_registry()
    responses_tools_to_chat_tools([{
        "type": "namespace", "name": "ns", "tools": [_fn("a"), _fn("b")],
    }])
    converted_output = [
        {"id": "c1", "type": "function_call", "call_id": "c1", "name": "a", "namespace": "ns", "arguments": "{}"},
        {"id": "c2", "type": "function_call", "call_id": "c2", "name": "b", "namespace": "ns", "arguments": "{}"},
    ]
    merged: list = []
    for recovered in responses_output_tool_calls(converted_output):
        merge_tool_call_payloads(merged, recovered)
    assert [t["name"] for t in merged] == ["ns__a", "ns__b"]
    assert [codex_function_call_item(t, i)["name"] for i, t in enumerate(merged)] == ["a", "b"]


def test_retry_recovers_custom_tool_call():
    reset_codex_tool_route_registry()
    responses_tools_to_chat_tools([{"type": "custom", "name": "apply_patch", "description": "d"}])
    converted_output = [{
        "id": "fc_9", "type": "custom_tool_call", "status": "completed",
        "call_id": "call_9", "name": "apply_patch", "input": "*** Begin Patch",
    }]
    calls = responses_output_tool_calls(converted_output)
    assert calls[0]["name"] == "apply_patch"
    merged: list = []
    for recovered in calls:
        merge_tool_call_payloads(merged, recovered)
    item = codex_function_call_item(merged[0], 0)
    assert item["type"] == "custom_tool_call"
    assert item["input"] == "*** Begin Patch"


def test_retry_ignores_reasoning_and_messages():
    out = _reasoning_only() + [{"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": "hi"}]}]
    assert responses_output_tool_calls(out) == []


# ------------------------------------------------------------ 配置项


def test_empty_turn_retries_defaults_to_one():
    assert ModelConfig.from_dict({"name": "m", "upstream_model": "m"}).empty_turn_retries == 1


def test_empty_turn_retries_configurable_and_clamped():
    assert ModelConfig.from_dict({"name": "m", "empty_turn_retries": 3}).empty_turn_retries == 3
    assert ModelConfig.from_dict({"name": "m", "empty_turn_retries": 0}).empty_turn_retries == 0
    assert ModelConfig.from_dict({"name": "m", "empty_turn_retries": -5}).empty_turn_retries == 0
    # 脏值必须回落到默认值, 不能把重试次数变成垃圾
    assert ModelConfig.from_dict({"name": "m", "empty_turn_retries": "abc"}).empty_turn_retries == 1
    assert ModelConfig.from_dict({"name": "m", "empty_turn_retries": "2"}).empty_turn_retries == 2
