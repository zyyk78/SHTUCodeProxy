"""收尾阶段的客户端断开不应刷 traceback。

背景 (线上问题):
上游流已正常结束、代理正在组装 SSE 收尾事件 (output_item.done /
function_call_arguments.delta / response.completed) 时, Codex 客户端可能
已经断开。此时 emit() 抛 _ClientDisconnectedError, 该异常从
handle_responses_streaming 冒泡到 handle_responses_post 的通用
except Exception, 于是整段 traceback 被打印到日志, 看起来像代理崩溃。

期望: 只记一行日志, 不打 traceback, 不再往已断开的 socket 回写 500。
"""
import proxy
from config_store import AppConfig, ModelConfig


class _FakeSock:
    def settimeout(self, _t):
        pass


class _FakeRFile:
    def __init__(self, payload):
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


class _FakeHandler(proxy.ProxyHandler):
    def __init__(self):
        # 绕过 BaseHTTPRequestHandler.__init__
        self.wfile = _FakeWFile()
        self.close_connection = False
        self.rfile = _FakeRFile(b'{"model":"deepseek-pro","input":"ping","stream":true}')
        self.headers = {}
        self.command = "POST"
        self.path = "/v1/responses"
        self.request_version = "HTTP/1.1"
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, code, message=None):
        self._status = code

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass


def _fake_config():
    model = ModelConfig(
        name="deepseek-pro",
        model_id="deepseek-pro",
        base_url="https://genaiapi.shanghaitech.edu.cn/v1/chat/completions",
        api_key="test-key",
        upstream_model="deepseek-pro",
        api_format="chat_completions",
    )
    cfg = AppConfig.default()
    cfg.models = [model]
    cfg.default_model_id = "deepseek-pro"
    cfg.timeout = 5

    def find_model(name):
        for m in cfg.models:
            if m.model_id == name or m.name == name:
                return m
        return cfg.models[0]

    cfg.find_model = find_model
    return cfg


def _run_with_finalize_disconnect(monkeypatched_logs):
    """模拟: 上游空内容 -> 收尾阶段 emit 抛客户端断开。"""
    handler = _FakeHandler()

    orig_write_sse = proxy.write_sse
    orig_write_data_sse = proxy.write_data_sse
    orig_send_headers = proxy.send_sse_headers
    orig_log_error = proxy.log_error
    orig_open_upstream = proxy.open_upstream
    orig_current_config = proxy.current_config
    try:
        proxy.send_sse_headers = lambda h: None
        proxy.write_data_sse = lambda h, data: True
        proxy.log_error = lambda msg: monkeypatched_logs.append(str(msg))
        proxy.current_config = lambda: _fake_config()

        # 前若干个 emit 成功 (流式正文阶段), 收尾阶段的 emit 抛客户端断开
        state = {"calls": 0}

        def fake_write_sse(h, event, payload):
            state["calls"] += 1
            # response.created / in_progress / 正文 delta 都成功,
            # 收尾阶段 (output_item.done / function_call_arguments.delta /
            # response.completed) 才断连。
            if event in ("response.created", "response.in_progress",
                         "response.output_text.delta", "response.output_item.added",
                         "response.content_part.added"):
                return True
            if event in ("response.output_text.done", "response.content_part.done"):
                return True
            return False

        proxy.write_sse = fake_write_sse

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":"hi"}}]}'

            def readline(self):
                return b""

        proxy.open_upstream = lambda *a, **k: _Resp()
        proxy.ProxyHandler.handle_responses_post(handler)
    finally:
        proxy.write_sse = orig_write_sse
        proxy.write_data_sse = orig_write_data_sse
        proxy.send_sse_headers = orig_send_headers
        proxy.log_error = orig_log_error
        proxy.open_upstream = orig_open_upstream
        proxy.current_config = orig_current_config
    return handler


def test_finalize_disconnect_logs_one_line_not_traceback():
    logs = []
    handler = _run_with_finalize_disconnect(logs)
    joined = "\n".join(logs)
    assert "Traceback" not in joined, f"客户端正常断连不应打印 traceback:\n{joined}"
    assert "disconnected during responses finalize" in joined, joined
    assert handler.close_connection is True


def test_finalize_disconnect_does_not_write_500_json():
    logs = []
    handler = _run_with_finalize_disconnect(logs)
    payload = b"".join(handler.wfile.written)
    assert b'"status_code": 500' not in payload
    assert b'"status_code":500' not in payload
