"""Regression coverage for Codex namespace/custom tool conversion.

背景 (线上事故):
codex-rs rust-v0.159 用 ToolSpec 序列化工具, MCP 工具和 multi-agent 工具
都是 {"type": "namespace", "name": ..., "tools": [...]}，而 apply_patch 是
{"type": "custom", ...}。SHTUCodeProxy 旧实现
`responses_tool_to_chat_tool()` 对 `type != "function"` 直接 `return None`,
于是 api_format=chat_completions (上科大 genaiapi 转换模式) 下
17 个工具只剩 11 个内置工具, MCP / multi-agent / web 能力全部静默消失。
minimax 转发模式因为原样透传 body 所以正常, Claude Code 走 anthropic 协议所以正常。
"""
from transformer import (
    codex_function_call_item,
    codex_tool_call_target,
    reset_codex_tool_route_registry,
    responses_namespace_flat_name,
    responses_request_to_chat_completions,
    responses_tool_route_map,
    responses_tool_to_chat_tool,
    responses_tools_to_chat_tools,
)


def _fn(name, description="desc"):
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": {}},
    }


BUILTIN_NAMES = (
    "exec_command",
    "write_stdin",
    "view_image",
    "update_plan",
    "request_user_input",
    "create_goal",
    "get_goal",
    "update_goal",
    "list_mcp_resources",
    "list_mcp_resource_templates",
    "read_mcp_resource",
)

MULTI_AGENT_NAMES = (
    "spawn_agent",
    "send_input",
    "wait_agent",
    "close_agent",
    "resume_agent",
)


def _codex_body():
    """按 codex-rs ToolSpec serde 的真实序列化结果构造 request body。"""
    tools = [_fn(name) for name in BUILTIN_NAMES]
    # ToolSpec::Freeform -> "custom" (apply_patch)
    tools.append(
        {"type": "custom", "name": "apply_patch", "description": "apply a patch", "format": {"type": "freeform"}}
    )
    # ToolSpec::WebSearch -> "web_search" (服务端内建, chat 上游不支持)
    tools.append({"type": "web_search", "external_web_access": True, "indexed_web_access": True})
    # ToolSpec::Namespace -> MCP 工具 (core/src/tools/handlers/mcp.rs)
    for ns, member in (
        ("mcp__github", "get_me"),
        ("mcp__filesystem", "read_text_file"),
        ("mcp__playwright", "browser_navigate"),
    ):
        tools.append({"type": "namespace", "name": ns, "description": ns, "tools": [_fn(member)]})
    # ToolSpec::Namespace -> multi_agent_v1 (multi_agents_spec.rs)
    tools.append(
        {
            "type": "namespace",
            "name": "multi_agent_v1",
            "description": "Tools for spawning and managing sub-agents.",
            "tools": [_fn(name) for name in MULTI_AGENT_NAMES],
        }
    )
    return {
        "model": "deepseek-pro",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "tools": tools,
        "stream": True,
    }


def _names(tools):
    return [tool["function"]["name"] for tool in tools]


# ---------------------------------------------------------------- namespace

def test_namespace_flat_name_matches_codex_rule():
    # codex-rs: format!("{namespace.rstrip('_')}__{name.lstrip('_')}")
    assert responses_namespace_flat_name("multi_agent_v1", "spawn_agent") == "multi_agent_v1__spawn_agent"
    assert responses_namespace_flat_name("mcp__github", "get_me") == "mcp__github__get_me"
    # 已带前缀时不重复叠加
    assert responses_namespace_flat_name("mcp__github", "mcp__github__get_me") == "mcp__github__get_me"
    # 空 namespace 退化为原名
    assert responses_namespace_flat_name("", "get_me") == "get_me"
    assert responses_namespace_flat_name(None, "get_me") == "get_me"


# ------------------------------------------------------- 事故复现 (旧行为)

def test_codex_body_converted_tool_count_is_20_not_11():
    body = _codex_body()
    assert len(body["tools"]) == 17
    converted = responses_tools_to_chat_tools(body["tools"])
    # 17 - 1(web_search 不支持) + ... 展开后共 20
    assert len(converted) == 20
    names = _names(converted)
    # 旧的 11 个内置工具一个都不能少
    for name in BUILTIN_NAMES:
        assert name in names
    # MCP 三个 server 的工具回来了
    assert "mcp__github__get_me" in names
    assert "mcp__filesystem__read_text_file" in names
    assert "mcp__playwright__browser_navigate" in names
    # multi-agent 五个工具回来了, 且用的是 codex 自己的扁平命名
    for name in MULTI_AGENT_NAMES:
        assert f"multi_agent_v1__{name}" in names
    # apply_patch (custom/freeform) 也回来了
    assert "apply_patch" in names
    # web_search 是服务端内建工具, chat 上游无法表达 -> 不发给模型, 也不静默
    assert "web_search" not in names
    assert not any("web_search" in name for name in names)


def test_converted_tools_are_all_chat_function_schema():
    for tool in responses_tools_to_chat_tools(_codex_body()["tools"]):
        assert tool["type"] == "function"
        assert isinstance(tool["function"]["name"], str) and tool["function"]["name"]
        assert isinstance(tool["function"]["parameters"], dict)


# --------------------------------------------------------- custom 降级

def test_custom_tool_degrades_to_single_string_input():
    out = responses_tool_to_chat_tool(
        {"type": "custom", "name": "apply_patch", "description": "apply a patch", "format": {"type": "freeform"}}
    )
    assert out is not None
    assert out["type"] == "function"
    assert out["function"]["name"] == "apply_patch"
    assert out["function"]["parameters"]["required"] == ["input"]
    assert out["function"]["parameters"]["properties"]["input"]["type"] == "string"


def test_function_tool_strips_responses_only_fields():
    out = responses_tool_to_chat_tool(
        {
            "type": "function",
            "name": "exec_command",
            "description": "run",
            "parameters": {"type": "object", "properties": {}},
            "strict": True,
            "output_schema": {"type": "object"},
            "defer_loading": True,
        }
    )
    assert out is not None
    for key in ("strict", "output_schema", "defer_loading"):
        assert key not in out["function"]


# ---------------------------------------------------- 未知格式: 丢弃+告警

def test_unknown_tool_type_is_dropped_not_raised():
    body = _codex_body()
    body["tools"].append({"type": "some_future_tool", "name": "quantum_search", "parameters": {}})
    converted = responses_tools_to_chat_tools(body["tools"])
    names = _names(converted)
    # 未知类型被丢弃, 但绝不抛异常/整个请求失败
    assert len(converted) == 20
    assert not any("quantum_search" in name for name in names)
    # 其余工具不受影响
    assert "mcp__github__get_me" in names
    assert "multi_agent_v1__spawn_agent" in names


def test_non_list_and_malformed_inputs_are_safe():
    assert responses_tools_to_chat_tools(None) == []
    assert responses_tools_to_chat_tools("not-a-list") == []
    assert responses_tools_to_chat_tools([None, 1, "x"]) == []
    assert responses_tool_to_chat_tool(None) is None
    assert responses_tool_to_chat_tool("nope") is None


def test_empty_namespace_is_dropped():
    converted = responses_tools_to_chat_tools([{"type": "namespace", "name": "mcp__empty", "tools": []}])
    assert converted == []


# -------------------------------------------------------------- 去重

def test_duplicate_names_are_deduplicated():
    tools = [
        _fn("mcp__github__get_me"),
        _fn("mcp__github__get_me"),
        {"type": "namespace", "name": "mcp__github", "description": "d", "tools": [_fn("get_me")]},
    ]
    names = _names(responses_tools_to_chat_tools(tools))
    assert names.count("mcp__github__get_me") == 1


# ------------------------------------------- 端到端: 走 request 转换入口

def test_request_level_conversion_keeps_namespace_tools():
    payload = responses_request_to_chat_completions(_codex_body(), "glm-chat")
    names = _names(payload["tools"])
    assert len(payload["tools"]) == 20
    assert "mcp__github__get_me" in names
    assert "multi_agent_v1__wait_agent" in names
    assert "apply_patch" in names
    assert "exec_command" in names


# ---------------------------------------------------------- 回程: 工具调用

def test_namespace_tool_call_round_trips_back_to_codex():
    """模型按扁平名调用后, 代理必须原样还原, codex 才能路由到 namespace 成员。"""
    from transformer import chat_completion_json_to_responses

    tools = responses_tools_to_chat_tools(_codex_body()["tools"])
    payload = {
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "multi_agent_v1__spawn_agent", "arguments": '{"message":"hi"}'},
                }],
            },
            "finish_reason": "tool_calls",
        }]
    }
    out = chat_completion_json_to_responses(payload, "glm-chat", 10, tools, False)
    calls = [item for item in out["output"] if item.get("type") == "function_call"]
    assert len(calls) == 1
    # WHY: codex 用 function_call item 的 (namespace, name) 查 handler 表
    # (router.rs::build_tool_call -> ToolName::new(namespace, name)),
    # namespace 是独立字段, 不从 name 拆。name 塞拍平名 -> unsupported call。
    assert calls[0]["name"] == "spawn_agent"
    assert calls[0]["namespace"] == "multi_agent_v1"
    assert calls[0]["arguments"] == '{"message":"hi"}'


def test_mcp_tool_call_round_trips_back_to_codex():
    from transformer import chat_completion_json_to_responses

    tools = responses_tools_to_chat_tools(_codex_body()["tools"])
    payload = {
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "mcp__github__get_me", "arguments": "{}"},
                }],
            },
            "finish_reason": "tool_calls",
        }]
    }
    out = chat_completion_json_to_responses(payload, "glm-chat", 10, tools, False)
    calls = [item for item in out["output"] if item.get("type") == "function_call"]
    assert [c["name"] for c in calls] == ["get_me"]
    assert [c["namespace"] for c in calls] == ["mcp__github"]


# ---------------------------------------------------------------- 回程解码


def test_route_map_is_built_from_raw_responses_tools():
    reset_codex_tool_route_registry()
    mapping = responses_tool_route_map(_codex_body()["tools"])
    assert mapping["multi_agent_v1__spawn_agent"] == ("multi_agent_v1", "spawn_agent", False)
    assert mapping["mcp__github__get_me"] == ("mcp__github", "get_me", False)
    # 内置工具也在表里, 但 namespace 为空 -> 解码后不输出 namespace 字段
    assert mapping["exec_command"] == ("", "exec_command", False)
    assert codex_tool_call_target("exec_command", mapping) == ("exec_command", None, False)
    # 未在表里的名字保持原样
    assert codex_tool_call_target("some_random_tool", mapping) == ("some_random_tool", None, False)


def test_builtin_tool_gets_no_namespace_field():
    reset_codex_tool_route_registry()
    responses_tools_to_chat_tools(_codex_body()["tools"])
    item = codex_function_call_item({"id": "c1", "name": "exec_command", "arguments": '{"cmd":"ls"}'}, 0)
    assert item["name"] == "exec_command"
    assert "namespace" not in item
    assert item["type"] == "function_call"


def test_ambiguous_flat_name_decodes_via_namespace_table():
    """mcp__github + get_me 拍平成 mcp__github__get_me, 盲拆会错成 mcp。"""
    reset_codex_tool_route_registry()
    tools = [{
        "type": "namespace",
        "name": "mcp__github",
        "tools": [{"type": "function", "name": "get_me", "description": "", "parameters": {}}],
    }]
    responses_tools_to_chat_tools(tools)
    assert codex_tool_call_target("mcp__github__get_me") == ("get_me", "mcp__github", False)


def test_explicit_registry_argument_overrides_global():
    """流式/非流式调用点可以显式传本次请求的表。"""
    reset_codex_tool_route_registry()
    explicit = {"multi_agent_v1__spawn_agent": ("multi_agent_v1", "spawn_agent", False)}
    item = codex_function_call_item(
        {"id": "c2", "name": "multi_agent_v1__spawn_agent", "arguments": "{}"},
        0,
        route_registry=explicit,
    )
    assert item["name"] == "spawn_agent"
    assert item["namespace"] == "multi_agent_v1"


# ------------------------------------------------- freeform / custom 工具回程


def test_custom_tool_call_is_restored_to_custom_tool_call_item():
    """apply_patch 等 type:custom 工具必须回 custom_tool_call。

    codex 的 apply_patch handler 只接受 ToolPayload::Custom, 收到
    Function payload 会直接回 "unsupported payload"。
    """
    reset_codex_tool_route_registry()
    tools = [{"type": "custom", "name": "apply_patch", "description": "apply a patch"}]
    chat_tools = responses_tools_to_chat_tools(tools)
    assert chat_tools[0]["function"]["name"] == "apply_patch"
    item = codex_function_call_item(
        {"id": "c3", "name": "apply_patch", "arguments": '{"input":"*** Begin Patch\\n*** End Patch"}'},
        0,
    )
    assert item["type"] == "custom_tool_call"
    assert item["name"] == "apply_patch"
    assert item["input"] == "*** Begin Patch\n*** End Patch"
    assert "arguments" not in item
    assert "namespace" not in item


def test_namespaced_custom_member_keeps_namespace():
    reset_codex_tool_route_registry()
    tools = [{
        "type": "namespace",
        "name": "editor",
        "tools": [{"type": "custom", "name": "write_file", "description": "write"}],
    }]
    responses_tools_to_chat_tools(tools)
    item = codex_function_call_item({"id": "c4", "name": "editor__write_file", "arguments": '{"input":"x"}'}, 0)
    assert item["type"] == "custom_tool_call"
    assert item["name"] == "write_file"
    assert item["namespace"] == "editor"
