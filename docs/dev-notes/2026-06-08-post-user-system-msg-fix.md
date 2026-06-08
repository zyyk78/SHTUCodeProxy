# Fix #006: Claude Code /model 切换 qwen-instruct/minimax 空响应

## 目标

修复 Claude Code 通过 /model 切换到 qwen-instruct、minimax 模型时返回空响应的问题。

## 验收标准

- Claude Code CLI `claude -p --model qwen-instruct "Say hello"` 正常输出
- Claude Code CLI `claude -p --model minimax "Say hello"` 正常输出
- Claude Code CLI `claude -p --model GPT-5.5 "Say hello"` 正常输出
- glm-chat 不受影响

## 影响范围

- `src/proxy.py` — `anthropic_messages_to_chat_completions` 函数
- `src/proxy.py` — `normalize_content_part`、`split_anthropic_content`、`anthropic_content_to_text`
- `src/proxy.py` — `sanitized_upstream_payload_for_model`、新增 `_strip_cache_control`

## 根因分析

### 问题 1（核心）：post-user system 消息导致上游空响应

Claude Code 在 user 消息之后会发送一条 `role=system` 的消息（包含 skill 提示）。这在 Anthropic API 中是合法的，但当代理将其转为 OpenAI chat_completions 格式后，qwen-instruct 上游 API 对此返回 HTTP 200 但 body 为空。

**调试过程**：
1. 直接 curl 上游 API → 正常返回 SSE 数据
2. 通过代理 → `iter_sse_lines` 返回零事件
3. dump 完整 payload 后直接发送 → 复现空响应
4. 二分法排查 → 3 条 messages 中的第 3 条 `role=system` 是根因
5. 将第 3 条 system 改为 user/assistant 或合并到首条 system → 正常

### 问题 2：thinking 块泄漏

`thinking`/`redacted_thinking` Anthropic 内容块在 `else` 分支被 `json.dumps` 序列化为文本转发上游。

### 问题 3：cache_control 导致 GPT-5.5 空流

`cache_control: {"type": "ephemeral"}` 是 Anthropic 专有字段，转发到 GPT-5.5 Responses API 时导致空流。

## 修复方案

1. **合并 post-user system 消息**：在 `anthropic_messages_to_chat_completions` 的消息合并阶段，检测 user/assistant 之后出现的 system 消息，合并到首条 system 消息末尾。若无首条 system 消息，则将 role 改为 user。

2. **跳过 thinking 块**：在 `normalize_content_part`、`split_anthropic_content`、`anthropic_content_to_text` 中添加 `thinking`/`redacted_thinking` 的 early return/pass。

3. **剥离 cache_control**：新增 `_strip_cache_control()` 递归函数，在 `sanitized_upstream_payload_for_model` 中对所有格式调用。

## 回归测试结果

| 模型 | Claude Code CLI | 结果 |
|------|----------------|------|
| GPT-5.5 | `claude -p --model GPT-5.5 "Say hello"` | ✅ Hello |
| qwen-instruct | `claude -p --model qwen-instruct "Say hello"` | ✅ Hello |
| minimax | `claude -p --model minimax "Say hello"` | ✅ Hello! |
| glm-chat | `claude -p --model glm-chat "Say hello"` | ✅ 👋 |
