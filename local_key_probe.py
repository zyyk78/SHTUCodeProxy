from __future__ import annotations

from config_store import DEFAULT_CHAT_COMPLETIONS_URL, load_config, save_config
from test_support.api_notes import DEFAULT_API_NOTES, require_tokens


def main() -> int:
    tokens = require_tokens(DEFAULT_API_NOTES)
    config = load_config()
    for model in config.models:
        if model.model_id in tokens:
            model.api_key = tokens[model.model_id]
            model.base_url = DEFAULT_CHAT_COMPLETIONS_URL if model.model_id == "glm-chat" else f"{DEFAULT_CHAT_COMPLETIONS_URL}/chat/completions"
            model.api_format = "chat_completions"
    save_config(config)
    print(f"Loaded {len(tokens)} local test token(s) into local config only. Do not package config.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
