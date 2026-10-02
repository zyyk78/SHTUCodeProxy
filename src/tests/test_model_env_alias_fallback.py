"""Regression coverage: model_env must not fall back to default_model_id.

背景: `AppConfig.from_dict` 曾把未配置的 MODEL_ENV_KEYS 全部 fallback 到
`default_model_id`，导致 `/v1/models` 里 "未配置就跳过" 的保护失效，
把 6 个 Claude 别名全部指向第一个模型并输出，模型列表刷屏。
"""
from config_store import AppConfig


def _base_config(**extra):
    return {
        "models": [
            {"name": "deepseek-pro", "upstream_model": "deepseek-pro",
             "base_url": "https://example.com/v1", "api_key": "k"},
            {"name": "glm-chat", "upstream_model": "glm-chat",
             "base_url": "https://example.com/v1", "api_key": "k"},
        ],
        **extra,
    }


def test_unset_model_env_keys_stay_empty():
    cfg = AppConfig.from_dict(_base_config())
    assert all(value == "" for value in cfg.model_env.values()), cfg.model_env


def test_unset_model_env_does_not_resolve_claude_aliases():
    cfg = AppConfig.from_dict(_base_config())
    # 没有显式 model_env 时，别名不应解析到第一个模型，而应走默认回退。
    assert cfg.find_model("claude-sonnet-4").model_id == cfg.default_model_id


def test_explicit_model_env_still_routes_alias():
    cfg = AppConfig.from_dict(_base_config(model_env={
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-chat",
    }))
    assert cfg.find_model("claude-sonnet-4").model_id == "glm-chat"
    assert cfg.find_model("claude-sonnet-4-20250514").model_id == "glm-chat"
    # 未配置的键仍然留空
    assert cfg.model_env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == ""


def test_partial_model_env_keeps_other_keys_empty():
    cfg = AppConfig.from_dict(_base_config(model_env={"ANTHROPIC_MODEL": "deepseek-pro"}))
    assert cfg.model_env["ANTHROPIC_MODEL"] == "deepseek-pro"
    assert cfg.model_env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == ""
    assert cfg.find_model("claude-opus-4").model_id == cfg.default_model_id
