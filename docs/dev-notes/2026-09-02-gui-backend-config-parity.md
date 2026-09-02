# GUI 后端配置同步

**日期**: 2026-09-02
**类型**: fix
**分支**: dev/fix-gui-backend-config-parity
**状态**: 🚧 进行中

## 目标

让 PyQt 前端完整读写当前后端配置模型，尤其是新增的全局 `tool_result_visible_fallback`，避免保存配置时静默丢失新字段。

## 验收标准

- [ ] GUI 显示并保存 `tool_result_visible_fallback`
- [ ] 模型编辑保留后端已有的 `max_context_tokens`、`stream_bridge`、`supports_reasoning`、`enable_thinking`
- [ ] 新增 GUI 同步回归测试通过
- [ ] 冒烟测试、模块导入和 py_compile 通过

## 影响范围

- 涉及文件：`src/pyqt_gui.py`, `tests/test_gui_config_parity.py`, `docs/CHANGELOG.md`, `docs/ISSUE-TRACKER.md`
- 风险评估：低，仅扩展界面与同步逻辑，不改变代理协议处理

## 实施记录

### Step 1: 登记与设计

- 改动：登记 #009，新增本开发记录。
- 验证：待完成。

## 验证结果

| 测试项 | 结果 |
|--------|------|
| 模块导入 | 待测 |
| 冒烟测试 | 待测 |
| 功能验证 | 待测 |
| 回归验证 | 待测 |

## 改动摘要

待完成。

## 回滚方案

还原本分支提交即可；不修改生产端口或运行时代理状态。
