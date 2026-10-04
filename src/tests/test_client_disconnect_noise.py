"""Regression coverage: downstream TLS disconnects are quiet and correctly labeled."""
import ssl

import proxy


class FakeFile:
    def __init__(self, exc):
        self.exc = exc

    def write(self, data):
        raise self.exc

    def flush(self):
        pass


class FakeHandler:
    def __init__(self, exc):
        self.wfile = FakeFile(exc)
        self.close_connection = False


def test_write_sse_returns_false_for_tls_bad_length():
    handler = FakeHandler(ssl.SSLError(1, "[SSL: BAD_LENGTH] bad length"))
    assert proxy.write_sse(handler, "response.output_text.delta", {"delta": "x"}) is False
    assert handler.close_connection is False


def test_write_sse_returns_true_for_normal_write():
    class OkFile:
        def write(self, data):
            pass

        def flush(self):
            pass

    handler = FakeHandler(None)
    handler.wfile = OkFile()
    assert proxy.write_sse(handler, "response.output_text.delta", {"delta": "x"}) is True


def test_non_tls_errors_are_not_swallowed():
    handler = FakeHandler(ValueError("serialization failure"))
    try:
        proxy.write_sse(handler, "response.output_text.delta", {"delta": "x"})
    except ValueError:
        pass
    else:
        raise AssertionError("non-TLS error was swallowed")


def test_tls_disconnect_is_classified_as_client_disconnect():
    assert proxy._is_client_disconnect(ssl.SSLError(1, "[SSL: BAD_LENGTH] bad length")) is True
    assert proxy._is_client_disconnect(ssl.SSLEOFError(1, "EOF occurred in violation of protocol")) is True
    assert proxy._is_client_disconnect(BrokenPipeError()) is True
    assert proxy._is_client_disconnect(ValueError()) is False


def test_responses_streaming_stops_without_failed_event_when_client_gone():
    import proxy

    data_sse_calls = []

    class Fake(proxy.ProxyHandler):
        def __init__(self):  # bypass BaseHTTPRequestHandler init
            self.close_connection = False

    class ModelConfig:
        model_id = "deepseek-pro"
        api_format = "chat_completions"
        stream_bridge = False

    orig_send_headers = proxy.send_sse_headers
    orig_write_sse = proxy.write_sse
    orig_write_data_sse = proxy.write_data_sse
    try:
        proxy.send_sse_headers = lambda handler: None
        proxy.write_sse = lambda handler, event, payload: False
        proxy.write_data_sse = lambda handler, data: data_sse_calls.append(data) or True
        handler = Fake.__new__(Fake)
        handler.close_connection = False
        proxy.ProxyHandler.handle_responses_streaming(
            handler,
            {"input": "ping"},
            {"model": "deepseek-pro", "input": "ping"},
            "token",
            "https://upstream.example/v1",
            5,
            ModelConfig(),
        )
    finally:
        proxy.send_sse_headers = orig_send_headers
        proxy.write_sse = orig_write_sse
        proxy.write_data_sse = orig_write_data_sse

    assert handler.close_connection is True
    assert data_sse_calls == []
