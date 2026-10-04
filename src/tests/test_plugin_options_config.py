"""插件配置外置: 部署相关路径/上限不再硬编码在插件源码里。

背景:
qwen_image 插件原先把 `MODEL_ROOT = /mnt/HDD1/llm/qwen-image-2.1` 写死在源码中,
既泄漏部署信息, 也让插件无法在别的机器上复用。改为从 config.json 的
plugins[].options 段注入, 并且**配置缺失时显式报不健康, 绝不猜路径**。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

from config_store import PluginConfig
from plugin_manager import RoutePlugin, load_route_plugins


# ------------------------------------------------------------ PluginConfig


def test_plugin_options_parsed_from_config():
    cfg = PluginConfig.from_dict({
        "module": "x.py", "enabled": True, "timeout": 600,
        "options": {"model_root": "/srv/model", "prompt_max": 42},
    })
    assert cfg.options == {"model_root": "/srv/model", "prompt_max": 42}
    assert cfg.to_dict()["options"]["model_root"] == "/srv/model"


def test_plugin_options_default_to_empty_dict():
    assert PluginConfig.from_dict({"module": "x.py"}).options == {}


def test_plugin_options_accept_settings_alias():
    assert PluginConfig.from_dict({"module": "x.py", "settings": {"a": 1}}).options == {"a": 1}


def test_plugin_options_ignores_non_dict():
    assert PluginConfig.from_dict({"module": "x.py", "options": "oops"}).options == {}


def test_plugin_options_survives_roundtrip():
    original = PluginConfig.from_dict({"module": "x.py", "options": {"k": "v"}})
    assert PluginConfig.from_dict(original.to_dict()).options == {"k": "v"}


# ------------------------------------------------------ configure() 钩子契约


def _cfg(plugin_dir, module_name, options=None):
    """构造一个最小 AppConfig 替身。WHY 用函数而不是 class:
    类体里赋值不会走闭包, 访问不到外层局部变量。"""
    class Cfg:
        pass
    Cfg.plugin_dir = str(plugin_dir)
    Cfg.plugins = [PluginConfig.from_dict({
        "module": module_name, "enabled": True, "options": options or {},
    })]
    return Cfg()


def test_plugin_manager_calls_configure_before_routes():
    """插件必须能声明 configure(options), 并在 routes() 之前拿到配置。"""
    with tempfile.TemporaryDirectory() as tmp:
        plugin_dir = Path(tmp) / "plugins"
        plugin_dir.mkdir()
        record = Path(tmp) / "calls.json"
        # WHY 记到文件而不是模块全局: spec_from_file_location 不会把模块注册进
        # sys.modules, 测试拿不到模块对象。文件副本能真实验证调用顺序。
        (plugin_dir / "order_probe.py").write_text(
            "import json, os\n"
            "from plugin_manager import RoutePlugin\n"
            "RECORD = os.environ['ORDER_PROBE_RECORD']\n"
            "def _log(entry):\n"
            "    data = []\n"
            "    if os.path.exists(RECORD):\n"
            "        data = json.load(open(RECORD))\n"
            "    data.append(entry)\n"
            "    json.dump(data, open(RECORD, 'w'))\n"
            "def configure(options):\n"
            "    _log(['configure', options])\n"
            "def routes():\n"
            "    _log(['routes'])\n"
            "    class R(RoutePlugin):\n"
            "        method = 'GET'\n"
            "        paths = ('/probe',)\n"
            "        def handle(self, handler, config, plugin):\n"
            "            return True\n"
            "    return [R()]\n",
            encoding="utf-8",
        )

        old_env = os.environ.get("ORDER_PROBE_RECORD")
        os.environ["ORDER_PROBE_RECORD"] = str(record)
        try:
            loaded = load_route_plugins(_cfg(plugin_dir, "order_probe.py", {"model_root": "/srv/m"}))
        finally:
            if old_env is None:
                os.environ.pop("ORDER_PROBE_RECORD", None)
            else:
                os.environ["ORDER_PROBE_RECORD"] = old_env

        assert len(loaded) == 1
        # options 已注入到 route
        assert loaded[0].plugin_options == {"model_root": "/srv/m"}
        calls = json.loads(record.read_text(encoding="utf-8"))
        # configure 确实在 routes 之前被调用, 且拿到了 options
        assert calls[0][0] == "configure"
        assert calls[0][1] == {"model_root": "/srv/m"}
        assert calls[1][0] == "routes"


def test_plugin_manager_reports_configure_failure():
    with tempfile.TemporaryDirectory() as tmp:
        plugin_dir = Path(tmp) / "plugins"
        plugin_dir.mkdir()
        (plugin_dir / "bad.py").write_text(
            "def configure(options):\n"
            "    raise ValueError('boom')\n"
            "def routes():\n"
            "    return []\n",
            encoding="utf-8",
        )
        with pytest.raises(Exception) as exc:
            load_route_plugins(_cfg(plugin_dir, "bad.py"))
        assert "configure" in str(exc.value)


def test_plugin_without_configure_still_loads():
    with tempfile.TemporaryDirectory() as tmp:
        plugin_dir = Path(tmp) / "plugins"
        plugin_dir.mkdir()
        (plugin_dir / "plain.py").write_text(
            "from plugin_manager import RoutePlugin\n"
            "class R(RoutePlugin):\n"
            "    method = 'GET'\n"
            "    paths = ('/plain',)\n"
            "    def handle(self, handler, config, plugin):\n"
            "        return True\n"
            "def routes():\n"
            "    return [R()]\n",
            encoding="utf-8",
        )
        assert len(load_route_plugins(_cfg(plugin_dir, "plain.py"))) == 1


# ------------------------------------------------------------- 插件自身行为


def _load_qwen(options):
    plugin_dir = Path(__file__).resolve().parents[1] / "plugins"
    sys.path.insert(0, str(plugin_dir))
    try:
        import qwen_image
    finally:
        sys.path.pop(0)
    qwen_image.configure(options)
    return qwen_image


def test_qwen_source_contains_no_hardcoded_local_paths():
    """源码里不能再出现任何部署相关的绝对路径。"""
    source = (Path(__file__).resolve().parents[1] / "plugins" / "qwen_image.py").read_text(encoding="utf-8")
    for needle in ("/mnt/", "/home/", "HDD1", "zyyk78"):
        assert needle not in source, f"插件源码里仍残留本地路径信息: {needle}"


def test_qwen_without_options_is_not_ready_and_reports_missing():
    qwen_image = _load_qwen({})
    assert qwen_image.SETTINGS.unconfigured() == ["model_root", "run_script"]
    assert qwen_image.SETTINGS.ready() is False


def test_qwen_with_options_becomes_ready(tmp_path):
    model_root = tmp_path / "model"
    (model_root / "scripts").mkdir(parents=True)
    script = model_root / "scripts" / "run.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    data_root = tmp_path / "data"

    qwen_image = _load_qwen({
        "model_root": str(model_root),
        "data_root": str(data_root),
        "run_script": "scripts/run.sh",
    })
    assert qwen_image.SETTINGS.unconfigured() == []
    assert qwen_image.SETTINGS.ready() is True
    assert qwen_image.SETTINGS.run_script == script
    assert qwen_image.SETTINGS.output_dir == data_root / "outputs"
    assert qwen_image.SETTINGS.upload_dir == data_root / "uploads"
    assert qwen_image.SETTINGS.log_dir == data_root / "logs"


def test_qwen_run_script_missing_file_is_not_ready(tmp_path):
    model_root = tmp_path / "model"
    model_root.mkdir()
    qwen_image = _load_qwen({
        "model_root": str(model_root),
        "data_root": str(tmp_path / "data"),
        "run_script": "scripts/run.sh",
    })
    assert qwen_image.SETTINGS.unconfigured() == []
    assert qwen_image.SETTINGS.ready() is False, "脚本不存在时不应报健康"


def test_qwen_options_override_limits(tmp_path):
    model_root = tmp_path / "model"
    (model_root / "scripts").mkdir(parents=True)
    (model_root / "scripts" / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    qwen_image = _load_qwen({
        "model_root": str(model_root), "data_root": str(tmp_path / "d"),
        "run_script": "scripts/run.sh", "prompt_max": 111, "upload_max_mb": 3,
    })
    assert qwen_image.SETTINGS.prompt_max == 111
    assert qwen_image.SETTINGS.upload_max_bytes == 3 * 1024 * 1024


def test_qwen_garbage_limits_fall_back_to_defaults(tmp_path):
    model_root = tmp_path / "model"
    (model_root / "scripts").mkdir(parents=True)
    (model_root / "scripts" / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    qwen_image = _load_qwen({
        "model_root": str(model_root), "data_root": str(tmp_path / "d"),
        "run_script": "scripts/run.sh", "prompt_max": "abc", "upload_max_mb": "xyz",
    })
    assert qwen_image.SETTINGS.prompt_max == 8000
    assert qwen_image.SETTINGS.upload_max_bytes == 50 * 1024 * 1024


def test_qwen_configure_updates_module_level_aliases(tmp_path):
    """内部逻辑仍在用这些模块级名字, configure() 后必须同步指向新值。"""
    model_root = tmp_path / "m2"
    (model_root / "scripts").mkdir(parents=True)
    (model_root / "scripts" / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    data_root = tmp_path / "d2"
    qwen_image = _load_qwen({
        "model_root": str(model_root), "data_root": str(data_root),
        "run_script": "scripts/run.sh", "prompt_max": 7,
    })
    assert qwen_image.OUTPUT_DIR == data_root / "outputs"
    assert qwen_image.UPLOAD_DIR == data_root / "uploads"
    assert qwen_image.LOG_DIR == data_root / "logs"
    assert qwen_image.PROMPT_MAX == 7
