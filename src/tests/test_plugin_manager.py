import proxy
from config_store import AppConfig
from plugin_manager import PluginError, load_route_plugins, route_plugins


class FakeHandler:
    def __init__(self, path):
        self.path = path

    def route_path(self):
        return self.path

    def check_ip(self):
        return True

    def check_auth(self):
        return True

    _HEALTH_PATHS = ("/", "/health", "/v1")


def test_plugin_config_round_trip():
    cfg = AppConfig.from_dict({
        "plugin_dir": "src/plugins",
        "plugins": [{"module": "comfy_workflow.py", "enabled": True, "timeout": 120}],
    })
    assert cfg.plugin_dir == "src/plugins"
    assert cfg.plugins[0].to_dict() == {
        "enabled": True, "module": "comfy_workflow.py", "timeout": 120,
    }


def test_plugin_loader_loads_routes():
    cfg = AppConfig.from_dict({
        "plugin_dir": "src/plugins",
        "plugins": [{"module": "comfy_workflow.py", "enabled": True}],
    })
    routes = load_route_plugins(cfg)
    assert [type(x).__name__ for x in routes] == [
        "ComfyGraphGet", "ComfyGraphSubmit",
        "ComfyWorkflowList", "ComfyWorkflowDocs", "ComfyWorkflowSurface",
        "ComfyHealth", "ComfyStatus", "ComfyResult", "ComfyDelete", "ComfyPurge",
    ]
    assert routes[5].auth_exempt is True
    assert routes[8].handles("DELETE", "/comfy/workflow/jobs/abc")
    assert routes[3].handles("GET", "/comfy/workflow/docs")
    assert routes[9].handles("POST", "/comfy/workflow/purge")


def test_missing_plugin_fails_closed():
    cfg = AppConfig.from_dict({
        "plugin_dir": "src/plugins",
        "plugins": [{"module": "missing_plugin.py", "enabled": True}],
    })
    try:
        load_route_plugins(cfg)
    except PluginError:
        pass
    else:
        raise AssertionError("missing plugin did not fail closed")


def test_proxy_dispatches_plugin_route_after_auth(monkeypatch):
    called = {}

    class P:
        method = "GET"
        paths = ("/comfy/test",)
        def handles(self, method, path):
            return method == "GET" and path == "/comfy/test"

        def handle(self, handler, config, plugin):
            called["ok"] = True
            return True

    monkeypatch.setattr(proxy, "route_plugins", lambda: [P()])
    handler = FakeHandler("/comfy/test")
    assert proxy.ProxyHandler.do_GET(handler) is None
    assert called["ok"] is True


def test_proxy_delete_dispatches_prefix_plugin(monkeypatch):
    called = {}

    class P:
        method = "DELETE"
        paths = ()
        prefixes = ("/comfy/workflow/jobs/",)
        def handles(self, method, path):
            return method == "DELETE" and path == "/comfy/workflow/jobs/abc"

        def handle(self, handler, config, plugin):
            called["ok"] = True
            return True

    monkeypatch.setattr(proxy, "route_plugins", lambda: [P()])
    handler = FakeHandler("/comfy/workflow/jobs/abc")
    assert proxy.ProxyHandler.do_DELETE(handler) is None
    assert called["ok"] is True
