"""Regression coverage for the unified model schema.

契约: 每个 model 只需 `name`(本地路由名) + `upstream_model`(上游真实模型名).
`model_id` 不再手写, 由 `name` 派生; 旧配置(仅 model_id / name+model_id)保持兼容.
"""
import json
import tempfile
from pathlib import Path

from config_store import AppConfig, ConfigError, ModelConfig, load_config


def test_unified_schema_derives_model_id_from_name():
    cfg = AppConfig.from_dict({"models": [
        {"name": "minimax3-claude", "upstream_model": "MiniMax-M3",
         "base_url": "https://api.minimaxi.com/anthropic", "api_key": "k"},
        {"name": "minimax3-codex", "upstream_model": "MiniMax-M3",
         "base_url": "https://api.minimax.cn", "api_key": "k"},
    ]})
    assert [m.model_id for m in cfg.models] == ["minimax3-claude", "minimax3-codex"]
    assert [m.name for m in cfg.models] == ["minimax3-claude", "minimax3-codex"]
    assert all(m.upstream_model == "MiniMax-M3" for m in cfg.models)


def test_distinct_names_route_independently_despite_same_upstream():
    cfg = AppConfig.from_dict({"models": [
        {"name": "minimax3-claude", "upstream_model": "MiniMax-M3",
         "base_url": "https://api.minimaxi.com/anthropic", "api_key": "k"},
        {"name": "minimax3-codex", "upstream_model": "MiniMax-M3",
         "base_url": "https://api.minimax.cn", "api_key": "k"},
    ]})
    a = cfg.find_model("minimax3-claude")
    b = cfg.find_model("minimax3-codex")
    assert a.base_url.endswith("/anthropic") and b.base_url == "https://api.minimax.cn"


def test_legacy_model_id_only_config_still_loads():
    cfg = AppConfig.from_dict({"models": [
        {"model_id": "glm-chat", "base_url": "x", "api_key": "k"},
    ]})
    m = cfg.models[0]
    assert m.name == "glm-chat" and m.model_id == "glm-chat" and m.upstream_model == "glm-chat"


def test_name_takes_precedence_over_model_id_for_routing():
    cfg = AppConfig.from_dict({"models": [
        {"name": "minimax3-c", "model_id": "MiniMax-M3", "base_url": "x", "api_key": "k"},
    ]})
    m = cfg.models[0]
    assert m.model_id == "minimax3-c"
    assert m.upstream_model == "MiniMax-M3"
    # 旧的上游名仍可作为请求别名命中
    assert cfg.find_model("MiniMax-M3") is m


def test_to_dict_omits_model_id_and_round_trips():
    cfg = AppConfig.from_dict({"models": [
        {"name": "minimax3-codex", "upstream_model": "MiniMax-M3", "base_url": "x", "api_key": "k"},
    ]})
    d = cfg.to_dict()
    assert all("model_id" not in item for item in d["models"])
    restored = AppConfig.from_dict(d)
    assert restored.models[0].model_id == "minimax3-codex"
    assert restored.models[0].upstream_model == "MiniMax-M3"


def test_duplicate_model_name_raises_config_error():
    cfg = AppConfig.from_dict({"models": [
        {"name": "dup", "upstream_model": "X", "base_url": "x", "api_key": "k"},
        {"name": "dup", "upstream_model": "Y", "base_url": "y", "api_key": "k"},
    ]})
    from config_store import ensure_unique_model_ids
    try:
        ensure_unique_model_ids(cfg)
    except ConfigError:
        pass
    else:
        raise AssertionError("duplicate model name should raise ConfigError")


def test_load_config_rejects_duplicate_models():
    payload = {"models": [
        {"name": "dup", "upstream_model": "X", "base_url": "x", "api_key": "k"},
        {"name": "dup", "upstream_model": "Y", "base_url": "y", "api_key": "k"},
    ]}
    path = Path(tempfile.mkdtemp()) / "config.json"
    path.write_text(json.dumps(payload))
    try:
        load_config(path)
    except ConfigError:
        pass
    else:
        raise AssertionError("load_config should raise ConfigError on duplicate names")


def test_load_config_accepts_unique_models():
    payload = {"models": [
        {"name": "minimax3-claude", "upstream_model": "MiniMax-M3", "base_url": "x", "api_key": "k"},
        {"name": "minimax3-codex", "upstream_model": "MiniMax-M3", "base_url": "y", "api_key": "k"},
    ]}
    path = Path(tempfile.mkdtemp()) / "config.json"
    path.write_text(json.dumps(payload))
    cfg = load_config(path)
    assert [m.model_id for m in cfg.models] == ["minimax3-claude", "minimax3-codex"]


def test_empty_model_id_falls_back_to_name_via_post_init():
    m = ModelConfig(name="foo", model_id="", base_url="x", api_key="",
                    upstream_model="Foo-Up", api_format="responses")
    assert m.model_id == "foo"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"OK: {fn.__name__}")
    print(f"all {len(fns)} passed")
