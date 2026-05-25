from __future__ import annotations

import re
from pathlib import Path
from typing import Dict


DEFAULT_API_NOTES = Path("D:/litellm/api.txt")


def tokens_from_api_notes(path: Path = DEFAULT_API_NOTES) -> Dict[str, str]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8-sig")
    labels = (
        ("GLM 5.1", "glm-chat"),
        ("deepv4", "deepseek-chat"),
        ("qwen3.5", "qwen-instruct"),
    )
    tokens: Dict[str, str] = {}
    for index, (label, model_id) in enumerate(labels):
        start = text.find(f"{label}:")
        if start < 0:
            continue
        end_candidates = [
            text.find(f"{next_label}:", start + len(label) + 1)
            for next_label, _ in labels[index + 1:]
        ]
        end_candidates = [position for position in end_candidates if position >= 0]
        section = text[start:min(end_candidates)] if end_candidates else text[start:]
        match = re.search(r"Authorization:\s*Bearer\s+([A-Za-z0-9._-]+)", section)
        if match:
            tokens[model_id] = match.group(1)
    return tokens


def require_tokens(path: Path = DEFAULT_API_NOTES) -> Dict[str, str]:
    tokens = tokens_from_api_notes(path)
    if not tokens:
        raise SystemExit(f"No test API tokens found in {path}. This helper is for local tests only and is excluded from release packages.")
    return tokens
