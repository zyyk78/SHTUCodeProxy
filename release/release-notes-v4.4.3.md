# SHTUCodeProxy v4.4.3

## 关键修复

- **Responses 流式真正逐 chunk 转发**：handle_responses_streaming 在收到上游 SSE delta 时即时转发 esponse.output_text.delta 事件，而非等待全部收完再一次性发送。客户端现在可以实时看到文字逐步出现。

- **修复 input_tokens: 0**：所有 estimate_value_tokens(body.get("input")) 替换为 estimate_anthropic_input_tokens(body)，正确估算 Responses 和 Messages 请求的输入 token 数。

- **修复 input 为字符串时 500 错误**：esponses_current_user_modalities 在 input 非 dict/list 时隐式返回 None，导致 "image" in None TypeError。增加 eturn set() 兜底。

- **修复 GPT-5.5 等原生 Responses 格式模型的流式空响应**：pply_auto_cache_control 会把 input: "hi" 转成带 cache_control 和 input_text 类型的复杂格式，非 Claude 上游不认识导致返回空。对 pi_format=responses 的模型跳过 cache_control 转换。

- **修复 handle_streaming 中 chat_stream_usage 变量未初始化**：stream_bridge 路径不经过 SSE 循环，变量不存在时触发 UnboundLocalError。

- **修复 estimate_anthropic_input_tokens 不识别 Responses input 字段**：原函数只处理 messages，新增对 input 字段（字符串/列表/字典）的估算支持。

## 已知问题

- qwen-instruct 的 /v1/messages 流式路径（stream_bridge 模式）可能返回空文本，非流式正常。建议通过 /v1/responses 路径使用 qwen-instruct。

## 升级说明

从 v4.4.2 升级：替换 EXE 或 ZIP 即可，配置文件无需修改。
