# MiniMax Codex Responses 本地路由名与旧运行时不兼容

**日期**: 2026-09-02  
**类型**: fix  
**分支**: 待创建（当前仅本地修复）  
**状态**: ✅ 已完成

## 目标

让 Codex 通过本地代理访问 MiniMax-M3 时，正确路由到 MiniMax 的 OpenAI Responses 端点。

## 验收标准

- [x] 本地代理同时接受 `/responses` 和 `/v1/responses`
- [x] MiniMax-M3 非流式请求返回 200 和 `status=completed`
- [x] 本地 `name=minimax3-c` 会被映射为上游 `model_id=MiniMax-M3`
- [x] 上游 URL 为 `https://api.minimax.cn/v1/responses`
- [x] Python 核心模块编译通过

## 影响范围

- 涉及文件：`src/config.json`（本地运行配置）、`src/proxy.py`（已有未提交透传拼接改动）
- 风险评估：低；仅调整 MiniMax 上游地址，不改变校园模型转换逻辑。

## 根因

用户真实配置中的 `https://api.minimax.cn` 是正确地址。实际有两层问题：8090 正在运行的进程是旧代码，所以本地 `/responses` 直接 404；当前源码虽支持 `/responses`，但透传时把本地路由名 `minimax3-c` 原样发给上游，而 MiniMax 只认识 `MiniMax-M3`。

## 实施记录

### Step 1: 复现

- 通过 8094 测试端口发送 `/responses` 与 `/v1/responses`
- 结果均为 404，日志显示上游为 `api.minimaxi.com/anthropic/v1/responses`

### Step 2: 修复

- 确认真实配置 `base_url=https://api.minimax.cn` 与 `name=minimax3-c` 可被当前路由匹配
- 复用当前 `proxy.py` 中兼容根式、`/v1` 式与全端点式 `base_url` 的透传拼接逻辑
- 透传前将 `payload["model"]` 映射为 `upstream_model` / `model_id`
- 增加 `exercise_passthrough_model_alias` 回归测试

### Step 3: 验证

| 测试项 | 结果 |
|--------|------|
| `/responses`（`minimax3-c`）非流式 | ✅ 200 / completed，上游 model=`MiniMax-M3` |
| `/v1/responses` 非流式 | ✅ 200 / completed |
| 模块编译 | ✅ |
| 测试进程关闭 | ✅ |

## 改动摘要

保留 MiniMax Responses 正确上游地址，并为透传补齐本地模型名到上游 model_id 的映射；8090 需重启加载新代码。

## 回滚方案

如需回滚代码，还原 `src/proxy.py` 中的透传模型名映射和端点拼接；配置中的 `https://api.minimax.cn` 不需要回滚。
