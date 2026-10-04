"""Small module-route plugin loader for SHTUCodeProxy.

A plugin module must expose `routes() -> list[RoutePlugin]`.
Routes are checked before built-in handlers (except IP policy). POST/DELETE
routes are checked after proxy auth.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, List, Optional


class RoutePlugin:
    method: str = "GET"
    paths: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ()
    auth_exempt: bool = False
    #: 来自 config.json 里该插件的 options 段, 由 load_route_plugins() 注入。
    plugin_options: dict = {}

    def handles(self, method: str, path: str) -> bool:
        methods = self.method if isinstance(self.method, (list, tuple)) else (self.method,)
        if method not in methods:
            return False
        return path in self.paths or any(path == p.rstrip("/") or path.startswith(p if p.endswith("/") else p + "/") for p in self.prefixes)

    def handle(self, handler: Any, config: Any, plugin: "RoutePlugin") -> bool:
        raise NotImplementedError


class PluginError(Exception):
    pass


_PLUGINS: List[RoutePlugin] = []


def load_route_plugins(config: Any) -> List[RoutePlugin]:
    global _PLUGINS
    _PLUGINS = []
    for item in getattr(config, "plugins", []):
        if not getattr(item, "enabled", False):
            continue
        module_name = getattr(item, "module", "")
        if not module_name:
            raise PluginError("plugin module is empty")
        plugin_dir = Path(getattr(config, "plugin_dir", "plugins")).expanduser().resolve()
        if not plugin_dir.exists():
            raise PluginError(f"plugin_dir does not exist: {plugin_dir}")
        candidates = []
        target = plugin_dir / module_name
        if target.is_file():
            candidates.append(target)
        else:
            py = target.with_suffix(".py")
            if py.is_file():
                candidates.append(py)
            pkg = target / "__init__.py"
            if pkg.is_file():
                candidates.append(pkg)
        if not candidates:
            raise PluginError(f"plugin module not found: {plugin_dir / module_name}")
        path = candidates[0]
        module_key = f"shtu_plugin_{path.stem}"
        spec = importlib.util.spec_from_file_location(module_key, path)
        if spec is None or spec.loader is None:
            raise PluginError(f"cannot load plugin: {path}")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise PluginError(f"plugin {path} failed to load: {exc}") from exc
        routes = getattr(module, "routes", None)
        if not callable(routes):
            raise PluginError(f"plugin {path} does not define routes()")
        # WHY: 让插件把部署相关值 (路径/上限等) 放进 config.json 的 options 段,
        # 而不是硬编码在插件源码里。必须在 routes() 之前调用, 插件才能用配置
        # 初始化自己的路径常量。
        options = dict(getattr(item, "options", None) or {})
        configure = getattr(module, "configure", None)
        if callable(configure):
            try:
                configure(options)
            except Exception as exc:
                raise PluginError(f"plugin {path} configure() failed: {exc}") from exc
        loaded = routes()
        if not isinstance(loaded, list):
            raise PluginError(f"plugin {path} routes() must return a list")
        for route in loaded:
            if not hasattr(route, "handle"):
                raise PluginError(f"plugin {path} returned invalid route without handle()")
            if hasattr(route, "name") and getattr(item, "name", ""):
                route.plugin_name = item.name
            route.plugin_timeout = getattr(item, "timeout", 600)
            route.plugin_options = options
        _PLUGINS.extend(loaded)
    return _PLUGINS


def route_plugins() -> List[RoutePlugin]:
    return _PLUGINS
