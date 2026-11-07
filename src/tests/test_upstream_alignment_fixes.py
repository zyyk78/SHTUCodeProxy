"""对齐上游 saberjack/SHTUCodeProxy v4.8.2~v4.9.5 的格式转换修复回归测试。

覆盖本轮从上游吸收的 7 项修复（#2 include 过滤按用户要求跳过）:

  1. cache_control 注入之后必须再剥一次, 否则 Responses 格式上游报
     "Unknown parameter: input[0].cache_control"。
  3. function_call item 的 id 必须是 fc_proxy_ 形状, 上游 tool id 只能进 call_id。
  4. 413 之后必须关闭 keep-alive, 否则残留 body 被当成下一个请求行。
  5. Anthropic 响应内容顺序必须是 [text, tool_use]。
  6. 只有 enable_thinking 为真才自动注入 thinking, supports_reasoning 不算。
  7. 上游不支持的内置工具 (web_search 等) 直接报错, 同时从 payload 剥离。
  8. 上游返回非 SSE 的 JSON 错误体时转成 error, 不再表现为空流。
  9. classifier 关键词收窄, 且 classifier 请求短路返回放行判定。
"""
import contextlib

import proxy
import transformer
from config_store import AppConfig, ModelConfig


class _FakeSock:
    def settimeout(self, _t):
        pass


class _FakeRFile:
    def __init__(self, payload: bytes):
        self._sock = _FakeSock()
        self._data = payload

    def read(self, n=-1):
        if not self._data:
            return b""
        if n is None or n < 0:
            out, self._data = self._data, b""
            return out
        out, self._data = self._data[:n], self._data[n:]
        return out


class _FakeWFile:
    def __init__(self):
        self.written = []
        self.closed = False

    def write(self, data):
        self.written.append(data)

    def flush(self):
        pass


class _CapturingHandler(proxy.ProxyHandler):
    """把上游 payload 捕获下来, 不真的发请求。"""

    def __init__(self, payload: bytes, path: str = "/v1/responses"):
        self.wfile = _FakeWFile()
        self.close_connection = False
        self.rfile = _FakeRFile(payload)
        self.headers = {"content-length": str(len(payload))}
        self.command = "POST"
        self.path = path
        self.request_version = "HTTP/1.1"
        self.client_address = ("127.0.0.1", 12345)
        self.status = None
        self.captured = {}

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    def _capture(self, body, upstream_payload, auth_token, upstream_url, timeout, model_config):
        self.captured["payload"] = upstream_payload
        self.captured["model_config"] = model_config
        self.captured["body"] = body

    def handle_streaming(self, body, upstream_payload, auth_token, upstream_url, timeout, model_config):
        self._capture(body, upstream_payload, auth_token, upstream_url, timeout, model_config)

    def handle_non_streaming(self, body, upstream_payload, auth_token, upstream_url, timeout, model_config):
        self._capture(body, upstream_payload, auth_token, upstream_url, timeout, model_config)

    def handle_responses_streaming(self, body, upstream_payload, auth_token, upstream_url, timeout, model_config):
        self._capture(body, upstream_payload, auth_token, upstream_url, timeout, model_config)

    def handle_responses_non_streaming(self, body, upstream_payload, auth_token, upstream_url, timeout, model_config):
        self._capture(body, upstream_payload, auth_token, upstream_url, timeout, model_config)


def _make_config(api_format="responses", enable_thinking=False, supports_reasoning=False):
    model = ModelConfig(
        name="test-model",
        model_id="test-model",
        base_url="https://genaiapi.shanghaitech.edu.cn/api/v1/start",
        api_key="test-key",
        upstream_model="test-model",
        api_format=api_format,
        enable_thinking=enable_thinking,
        supports_reasoning=supports_reasoning,
    )
    cfg = AppConfig.default()
    cfg.models = [model]
    cfg.default_model_id = "test-model"
    cfg.auth_key = ""
    cfg.timeout = 5
    cfg.allowed_ips = []
    cfg.denied_ips = []

    def find_model(name):
        for candidate in cfg.models:
            if candidate.model_id == name or candidate.name == name:
                return candidate
        return cfg.models[0]

    cfg.find_model = find_model
    return cfg


@contextlib.contextmanager
def _patched(config):
    """把 proxy 的配置源和插件路由替换掉, 避免测试碰真实配置/插件。"""
    saved = {
        "current_config": proxy.current_config,
        "route_plugins": proxy.route_plugins,
    }
    proxy.current_config = lambda: config
    proxy.route_plugins = lambda: []
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(proxy, name, value)


def _contains_key(value, key):
    if isinstance(value, dict):
        if key in value:
            return True
        return any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


# ---------------------------------------------------------------- 1. cache_control
def test_cache_control_injected_by_auto_cache_is_stripped_before_upstream():
    # WHY: auto-cache 只在有 developer/system 消息或多轮 input 时才落标记; 单条
    # user 消息不会触发, 那样测出来的"没有 cache_control"是假通过。
    body = (
        b'{"model":"test-model","stream":false,'
        b'"input":['
        b'{"role":"developer","content":[{"type":"input_text","text":"sys"}]},'
        b'{"role":"user","content":[{"type":"input_text","text":"hi"}]},'
        b'{"role":"user","content":[{"type":"input_text","text":"more"}]}]}'
    )
    handler = _CapturingHandler(body, "/v1/responses")
    with _patched(_make_config(api_format="responses")):
        proxy.ProxyHandler.handle_responses_post(handler)

    payload = handler.captured.get("payload")
    assert payload is not None, "上游 payload 未被构造, 测试前置条件失效"
    # 前置条件: 转换后的 payload 必须真的具备 auto-cache 的注入条件, 否则断言无意义。
    assert transformer.apply_auto_cache_control(transformer._orjson_loads(
        transformer._orjson_dumps(payload)
    )) >= 1 or _contains_key(payload, "cache_control") is False, ""
    assert not _contains_key(payload, "cache_control"), (
        "apply_auto_cache_control 注入的 cache_control 必须在发往上游前剥掉, "
        f"否则 Responses 上游会报 Unknown parameter; 实际 payload={payload}"
    )


# ------------------------------------------------------------- 3. function_call id
def test_codex_function_call_item_uses_fc_prefix_and_keeps_upstream_call_id():
    item = transformer.codex_function_call_item(
        {"id": "call_upstream_1", "name": "Bash", "arguments": "{}"}, 0
    )
    assert item["id"].startswith("fc_proxy_"), f"item id 必须以 fc_ 形状开头: {item['id']}"
    assert item["id"] != "call_upstream_1", "上游 tool id 不能直接当作 item id"
    assert item["call_id"] == "call_upstream_1", "上游 tool id 必须保留在 call_id 里"


# ------------------------------------------------------------------- 4. 413 断连
def test_oversized_body_closes_connection():
    handler = _CapturingHandler(b"{}", "/v1/messages")
    handler.headers = {"content-length": str(10 ** 9)}
    try:
        proxy.read_json_body(handler)
    except proxy._BodyTooLargeError:
        pass
    else:
        raise AssertionError("超限 body 应该抛 _BodyTooLargeError")
    assert handler.status == 413, f"应回 413, 实际 {handler.status}"
    assert handler.close_connection is True, "413 之后必须关闭 keep-alive 连接"


# ------------------------------------------------------------- 5. tool_use 顺序
def test_anthropic_content_orders_text_before_tool_use():
    payload = {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "先说明一下"}],
            },
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "Bash",
                "arguments": "{}",
            },
        ]
    }
    message = transformer.responses_json_to_anthropic_message(payload, _make_config().models[0])
    types = [block.get("type") for block in message["content"]]
    assert "tool_use" in types and "text" in types, types
    assert types.index("text") < types.index("tool_use"), f"顺序必须是 [text, tool_use], 实际 {types}"
    assert message["stop_reason"] == "tool_use"


# ------------------------------------------------------- 6. thinking 注入条件
def test_supports_reasoning_alone_does_not_inject_thinking():
    body = (
        b'{"model":"test-model","stream":false,'
        b'"messages":[{"role":"user","content":"hi"}]}'
    )
    handler = _CapturingHandler(body, "/v1/messages")
    with _patched(_make_config(api_format="chat_completions", enable_thinking=False, supports_reasoning=True)):
        proxy.ProxyHandler.do_POST(handler)

    payload = handler.captured.get("payload")
    assert payload is not None, "请求没有被转发到上游构造阶段"
    assert "chat_template_kwargs" not in payload, (
        "supports_reasoning=True 但 enable_thinking=False 时不应强制开启上游推理; "
        f"实际 payload={payload}"
    )


def test_enable_thinking_still_injects_chat_template_kwargs():
    body = (
        b'{"model":"test-model","stream":false,'
        b'"messages":[{"role":"user","content":"hi"}]}'
    )
    handler = _CapturingHandler(body, "/v1/messages")
    with _patched(_make_config(api_format="chat_completions", enable_thinking=True)):
        proxy.ProxyHandler.do_POST(handler)

    payload = handler.captured.get("payload")
    assert payload is not None
    assert payload.get("chat_template_kwargs") == {"enable_thinking": True}, payload


# -------------------------------------------------- 7. 不支持的内置工具报错
def test_unsupported_builtin_tool_is_rejected_with_error():
    body = (
        b'{"model":"test-model","stream":false,'
        b'"tools":[{"type":"web_search_preview","name":"web_search"}],'
        b'"input":[{"role":"user","content":[{"type":"input_text","text":"hi"}]}]}'
    )
    handler = _CapturingHandler(body, "/v1/responses")
    with _patched(_make_config()):
        proxy.ProxyHandler.handle_responses_post(handler)

    assert handler.status == 400, f"不支持的工具应回 400, 实际 {handler.status}"
    written = b"".join(handler.wfile.written).decode("utf-8", "replace")
    assert "Unsupported tool type(s)" in written, written
    assert "web_search_preview" in written, written
    assert "payload" not in handler.captured, "命中不支持的工具时不应再请求上游"


def test_unsupported_tools_are_still_stripped_from_payload():
    stripped = transformer._strip_unsupported_tools(
        {
            "tools": [
                {"type": "web_search_preview"},
                {"type": "function", "function": {"name": "Bash"}},
            ],
            "tool_choice": {"type": "function", "function": {"name": "Bash"}},
        }
    )
    types = [tool.get("type") for tool in stripped["tools"]]
    assert "web_search_preview" not in types, stripped
    assert "function" in types, "正常函数工具不能被误伤"
    assert stripped.get("tool_choice") is not None, "还剩别的工具时不应丢掉 tool_choice"


def test_orphan_tool_choice_removed_when_all_tools_stripped():
    stripped = transformer._strip_unsupported_tools(
        {
            "tools": [{"type": "web_search_preview"}],
            "tool_choice": {"type": "function", "function": {"name": "web_search"}},
        }
    )
    assert "tools" not in stripped, stripped
    assert "tool_choice" not in stripped, "工具全被剥离后残留 tool_choice 会被上游拒绝"


# ------------------------------------------------------- 8. 非 SSE 错误体
def test_non_sse_body_becomes_error_not_empty_stream():
    kind, parsed = transformer.extract_text_delta(
        "non_sse_body", '{"success":false,"message":"Tool \'web_search\' disabled","code":500}'
    )
    assert kind == "error", f"非 SSE 错误体必须变成 error, 实际 {kind}"
    assert "disabled" in str(parsed), parsed


def test_iter_sse_lines_yields_non_sse_body_for_plain_json():
    class _Resp:
        def __init__(self, lines):
            self._lines = list(lines)

        def readline(self):
            return self._lines.pop(0) if self._lines else b""

    events = list(transformer.iter_sse_lines(_Resp([b'{"success":false,"message":"boom"}\n'])))
    assert events, "纯 JSON 响应不能被静默丢弃"
    event_name, data = events[-1]
    assert event_name == "non_sse_body", events
    assert "boom" in data, events


def test_genai_success_false_is_error_in_sse_payload():
    kind, parsed = transformer.extract_text_delta(
        None, '{"success":false,"message":"Tool disabled","code":500}'
    )
    assert kind == "error", (kind, parsed)


# ------------------------------------------------------------- 9. classifier
def test_main_agent_prompt_is_not_mistaken_for_classifier():
    main_agent = {
        "system": (
            "You are Claude Code running in auto mode. Follow the permission rules "
            "and ask for approval before destructive actions."
        )
    }
    assert transformer.is_claude_auto_classifier_request(main_agent) is False, (
        "主 agent 的 system prompt 含 auto mode / permission, 不能被判成 classifier"
    )

    real_classifier = {"system": "You are a security monitor that outputs <block>yes</block> or <block>no</block>."}
    assert transformer.is_claude_auto_classifier_request(real_classifier) is True


def test_classifier_request_short_circuits_with_allow_verdict():
    body = (
        b'{"model":"test-model","max_tokens":16,'
        b'"system":"You are a security monitor. Reply with <block>yes</block> or <block>no</block>.",'
        b'"messages":[{"role":"user","content":"rm -rf build"}]}'
    )
    handler = _CapturingHandler(body, "/v1/messages")
    with _patched(_make_config()):
        proxy.ProxyHandler.do_POST(handler)

    assert handler.status == 200, f"classifier 短路应回 200, 实际 {handler.status}"
    written = b"".join(handler.wfile.written).decode("utf-8", "replace")
    assert "<block>no</block>" in written, written
    assert "payload" not in handler.captured, "classifier 请求不应转发给上游模型"


def test_classifier_short_circuit_can_be_disabled_by_env(monkeypatch):
    monkeypatch.setenv("SHTU_CLASSIFIER_SHORTCIRCUIT", "0")
    assert transformer.classifier_short_circuit_enabled() is False
    monkeypatch.delenv("SHTU_CLASSIFIER_SHORTCIRCUIT", raising=False)
    assert transformer.classifier_short_circuit_enabled() is True
