# GUI 后端配置同步

**日期**: 2026-09-02
**类型**: fix
**分支**: dev/fix-gui-backend-config-parity
**状态**: ✅ 已完成

## 目标

让 PyQt 前端完整读写当前后端配置模型，尤其是新增的全局 `tool_result_visible_fallback`，避免保存配置时静默丢失新字段。

## 验收标准

- [x] GUI 显示并保存 `tool_result_visible_fallback`
- [x] 模型编辑保留后端已有的 `max_context_tokens`、`stream_bridge`、`supports_reasoning`、`enable_thinking`
- [x] 新增 GUI 同步回归测试通过
- [x] 模块导入和 py_compile 通过；完整冒烟在既有的 auto-cache 断言处失败（历史遗留，与本次改动无关）

## 影响范围

- 涉及文件：`src/pyqt_gui.py`, `tests/test_gui_config_parity.py`, `docs/CHANGELOG.md`, `docs/ISSUE-TRACKER.md`
- 风险评估：低，仅扩展界面与同步逻辑，不改变代理协议处理

## 实施记录

### Step 1: 登记与设计

- 改动：登记 #009，新增本开发记录。
- 验证：`docs/ISSUE-TRACKER.md` 已新增 #009。

### Step 2: 前端同步与回归测试

- 改动：Server 区新增 `Tool result fallback` 开关；`sync_server_fields` 将其写入 `AppConfig.tool_result_visible_fallback`。模型编辑继续显式保留未暴露的 `max_context_tokens` / `stream_bridge`，并将 Thinking 状态同步到 `supports_reasoning` / `enable_thinking`。
- 验证：新增 PyQt GUI 冒烟断言，覆盖新开关加载、关闭、保存、开启、再次保存。

## 验证结果

| 测试项 | 结果 |
|--------|------|
| 模块导入 | ✅ |
| py_compile | ✅ |
| 功能验证（GUI 配置保存） | ✅ |
| 回归验证（targeted pytest） | ✅ |
| 完整冒烟测试 | ⚠️ 在既有 `Codex Chat payload should get automatic cache boundaries` 失败；该失败早于本次改动，与 GUI 配置保存无关 |

## 改动摘要

为最新后端配置字段补齐 GUI 展示与保存路径，避免 GUI 保存动作把后端新增开关静默重置；同时保留未在界面上直接编辑的模型字段。

## 回滚方案

还原本分支提交即可；不修改生产端口或运行时代理状态。
