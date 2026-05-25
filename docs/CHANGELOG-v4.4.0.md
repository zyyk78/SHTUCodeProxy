# SHTUCodeProxy v4.4.0 变更记录

## 2026-05-25 v4.4.0

### 源码目录重构
- 将 `cli.py`、`config_store.py`、`linux_launcher.py`、`platform_utils.py`、`proxy.py`、`pyqt_gui.py`、`safe_io.py` 从根目录移入 `src/` 子目录
- 更新 `app.py` 添加 `sys.path` 引入 `src/`
- 更新 `build_exe.ps1` 和 `build_unix.sh` 的 `--add-data` 路径和 `--paths src` 参数
- 修复 `build_exe.ps1` 中 `--paths src` 与 `app.py` 参数分隔 bug

### Bug 修复
- **P0-1**: 修复 `apply_auto_cache_control` 对 Responses 格式 payload 失效的问题，增加 `input` 字段分支，Codex 走 `/v1/responses` 端点时自动缓存控制现在正常工作
- **P0-2**: 修复 `stop_reason_from_done` 对 `response.completed` 事件提取 stop_reason 不完整的问题，支持 Responses 格式的顶层和嵌套结构，正确识别 `tool_use`/`max_tokens`/`end_turn`
- **P0-3**: 修复 Anthropic Messages 端点（`handle_streaming`）对 `chat_completions` 格式模型（Qwen/GLM）缺少非流式 bridge 的问题，与 Responses 端点行为一致
- **P1-2**: 添加 `/v1/models` 和 `/models` GET 端点，返回当前配置的模型列表，改善 Claude Code 启动兼容性

### 功能改进
- **P1-7**: 将 Qwen stream bridge 的硬编码检测（`"qwen" in model_id`）改为 `ModelConfig.stream_bridge` 配置项，支持显式开关且默认根据 model_id 自动推断

### 仓库整理
- 将构建脚本移入 `build/`，文档移入 `docs/`，测试移入 `tests/`
- 移除根目录下的 API probe 脚本和 v1.x/v2.0 旧版 release 产物
- 添加 `docs/PROJECT-INDEX.md` 项目目录索引和模块依赖关系图
- 添加 `docs/CHANGELOG-v4.3.3.md` v4.3.3 变更记录
- 更新 README.md 版本引用

### Git 提交历史
- `c215f9c` src/config_store.py - ModelConfig.stream_bridge field for configurable stream bridge; src/proxy.py - replace hardcoded qwen detection with stream_bridge config
- `7e489dd` src/proxy.py - fix cache control for Responses payload, add Qwen bridge to Anthropic streaming, add /v1/models endpoint, enhance stop_reason extraction
- `928dce2` README.md - update version references from v4.3.2 to v4.3.3
- `8e5e1c2` docs/CHANGELOG-v4.3.3.md: v4.3.3 src/ refactor changelog; docs/PROJECT-INDEX.md: project directory index and module dependency map
- `808d0b8` src/ - application source modules: proxy server, CLI, PyQt5 GUI, config store, platform utils, safe I/O, Linux launcher
- `c3bcb27` build/ - Windows/Linux build scripts and app icon; docs/ - changelog, config examples, license, security policy; tests/ - smoke test and API notes; remove unused cli_app.py and gui.py
- `5c6a050` remove obsolete API probe scripts and v1.x/v2.0 release binaries from repo
