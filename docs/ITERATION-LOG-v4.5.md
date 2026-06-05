# SHTUCodeProxy v4.5.0 - v4.5.1 Iteration Log

**Date**: 2026-06-05
**Scope**: Fix Codex /compact, Responses API compatibility, add missing route handlers

## Problem Timeline

### 1. Codex /compact fails with "stream disconnected before completion"

**Symptom**: Codex CLI `/compact` command returns empty stream error when using glm-chat model.

**Root Cause 1** (v4.5.0 fix): `tool_choice: auto` sent when `tools` list is empty
- Upstream rejects contradictory `tool_choice: auto` + empty `tools`
- Fix: only include `tool_choice` when `tools` is non-empty
- File: `src/proxy.py` responses_request_to_chat_completions()

**Root Cause 2** (v4.5.0 fix): `content: None` in assistant tool_call messages
- glm-chat upstream cannot parse `content: None`, expects `content: ""`
- Error: `'NoneType' object is not iterable`
- Fix: replaced `"content": None` with `"content": ""` in function_call message construction
- Also fixed `"content": text or None` -> `"content": text or ""` in anthropic path
- File: `src/proxy.py` line ~1494, ~1249

**Root Cause 3** (v4.5.0 fix): `thinking` parameter passthrough to non-Anthropic upstreams
- GPT-5.5, GLM, DeepSeek don't support `thinking` parameter
- Returns "Unknown parameter: thinking" error, causing empty stream
- Fix: strip `thinking` parameter in request sanitization
- File: `src/proxy.py`

**Root Cause 4** (v4.5.1 fix): `tool_choice` dict not converted to string
- Codex sends `tool_choice: {"type": "auto"}` via Responses API
- `responses_tool_choice_to_chat()` passed dict through unchanged
- chat_completions upstream expects string "auto", not dict
- Fix: map `{"type": "auto"}` -> `"auto"`, `{"type": "none"}` -> `"none"`, `{"type": "required"}` -> `"required"`
- File: `src/proxy.py` responses_tool_choice_to_chat()

### 2. Claude Code returns empty content with GPT-5.5 and DeepSeek models

**Symptom**: Claude Code in VS Code gets empty stream responses for GPT-5.5 and DeepSeek, but glm-chat works fine.

**Root Cause**: Same as Root Cause 4 above - tool_choice dict-to-string conversion bug affects Messages API path when proxy converts Anthropic format to chat_completions.

### 3. Codex Desktop 404 on certain routes

**Symptom**: Codex Desktop sends requests to `/codex/v1/responses` instead of `/v1/responses`, getting 404.

**Fix**: Added route aliases and handlers:
- `/codex/v1/responses` -> routes to handle_responses_post()
- `POST /v1/responses/{id}/input_items` -> returns minimal completed response (stateless proxy)
- `POST /v1/responses/{id}/cancel` -> returns cancelled status
- `GET /v1/responses/{id}` -> returns 404 with "stateless proxy" message

### 4. Context window config issues

**Symptom**: Codex config.toml missing `model_context_window` and `model_auto_compact_token_limit` after fresh install.

**Fix**:
- Added auto-inference of `max_context_tokens` for known models via `default_max_context_tokens()`
- Added automatic injection of context window and auto-compact settings into Codex config.toml
- Preserved user-customized values on Save+Connect (don't overwrite if model unchanged)

## Version Changes

### v4.5.0 (2026-06-05)
- Added /compact endpoint handling
- Added max_context_tokens per model
- Added auto context window injection into Codex config
- Fixed content: None -> content: ""
- Fixed tool_choice when tools empty
- Fixed thinking parameter stripping
- Fixed consecutive same-role messages merging
- Fixed function_call batching
- Updated GLM default context to 200000

### v4.5.1 (2026-06-05)
- Fixed tool_choice dict-to-string conversion (P0)
- Added /codex/v1/responses route alias (P1)
- Added /v1/responses/{id}/input_items handler (P1)
- Added /v1/responses/{id}/cancel handler (P1)
- Added GET /v1/responses/{id} handler (P1)

## Model Context Window Defaults

| Model | max_context_tokens |
|-------|-------------------|
| GPT-5.5 | 400000 |
| deepseek-chat | 192000 |
| deepseek-pro | 128000 |
| glm-chat | 200000 |
| qwen-instruct | 131072 |

Auto-compact threshold = 90% of max_context_tokens.

## Test Results

### v4.5.1 API Tests: 23/23 PASS
- GET endpoints: 2/2
- New route handlers: 3/3
- Messages API (all 5 models): 5/5
- Responses API (all 5 models): 5/5
- /compact (all 5 models): 5/5
- Tool choice dict-to-string: 2/2
- Streaming: 2/2

## Release Assets (9 total)

1. SHA256SUMS-v4.5.1.txt
2. SHTUCodeProxy-v4.5.1-source.zip
3. SHTUCodeProxy-v4.5.1-source-linux-macos.zip
4. SHTUCodeProxy-v4.5.1-windows-x64.exe
5. SHTUCodeProxy-v4.5.1-windows-x64.zip
6. SHTUCodeProxy-v4.5.1-linux-x86_64
7. SHTUCodeProxy-v4.5.1-linux-x86_64-headless-cli.zip
8. SHTUCodeProxy-v4.5.1-linux-x86_64-python-launcher.tar.xz
9. shtucodeproxyctl-v4.5.1-linux-x86_64

## Key Files Modified

| File | Changes |
|------|---------|
| `src/proxy.py` | tool_choice conversion, route handlers, content: None fix, thinking strip, compact support |
| `src/config_store.py` | default_max_context_tokens(), context window injection, preserve user settings |
| `src/cli.py` | Codex config.toml context window injection |
| `docs/CHANGELOG.md` | v4.5.0 and v4.5.1 changelog entries |
| `VERSION` | Updated to 4.5.1 |

## Commits (v4.5.0 - v4.5.1)

- `bb81b86` docs: update CHANGELOG for v4.5.1
- `785a0f4` fix: strip thinking parameter to prevent empty responses on non-Anthropic upstreams
- `91132c9` fix: auto-infer max_context_tokens for known models when not in config
- `8128884` fix: preserve user-customized context tokens on Save+Connect, update model defaults
- `c55db0c` v4.5.1: fix responses_tool_choice_to_chat dict-to-string conversion
- `73507db` feat: add Codex route handlers for /codex/v1/responses, input_items, cancel, and GET /v1/responses/{id}
