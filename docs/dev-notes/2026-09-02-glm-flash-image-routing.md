# GLM Flash 图片路由排查

**日期**: 2026-09-02
**类型**: fix
**分支**: dev/fix-glm-flash-image-routing
**状态**: ✅ 已完成

## 目标

确认 `glm-5.3-flash` 图片输入失败原因，并让 Codex/Claude 图片请求按能力正确转发到 Chat Completions。

## 验收标准

- [x] 复现并记录当前请求被降级或丢失图片的具体路径
- [x] Responses 与 Anthropic Messages 图片输入都能生成 `image_url` Chat 消息
- [x] 文本-only 模型仍保持现有图片降级行为
- [x] 目标回归与 pytest 用例通过；完整冒烟仍受历史 auto-cache 断言影响

## 影响范围

- 涉及文件：`src/proxy.py`, `src/transformer.py`, `tests/`
- 风险评估：中，涉及多模态降级与模型能力识别

## 实施记录

### Step 1: 复现与登记

- 改动：登记 #010，新增本开发记录。
- 验证：本地转换实验确认 `supports_image=true` 时 Anthropic/Responses 图片块均能转换成 `image_url`；当前本地 `glm-chat.supports_image=false` 时会替换为占位文本。

### Step 2: 修复能力识别

- 改动：在 Anthropic Messages 路径镜像 Codex Responses 路径的逻辑——当前用户消息包含图片且配置标记不支持图片时，仅在本次请求运行时将 `supports_image` 修正为 true。随后确认 `glm-chat` 就是 GLM 5.3 Flash，因此默认能力表也加入 `glm-chat` / `glm-5.3-flash`，并修正本地配置中显式的 false。
- 验证：`glm-5.3-flash` 的 Claude base64 图片和 Codex `input_image` 都生成 Chat `image_url`；text-only 模型仍降级。

## 验证结果

| 测试项 | 结果 |
|--------|------|
| 模块导入 | ✅ |
| py_compile | ✅ |
| 功能验证（GLM flash 图片双协议转换） | ✅ |
| 功能验证（glm-chat 默认图片能力） | ✅ |
| 回归验证（targeted pytest） | ✅ 16 passed |
| 完整冒烟测试 | ⚠️ 仍在历史 `Codex Chat payload should get automatic cache boundaries` 失败，与本修复无关 |

## 改动摘要

修复 `glm-chat` / `glm-5.3-flash` 的多模态能力识别：默认能力表确认 `glm-chat` 映射 GLM 5.3 Flash 并允许图片；Anthropic 路径增加请求级能力修正；本地配置中显式 false 已改为 true。显式 `supports_image=false` 仍然优先，text-only 模型降级保护保持不变。

## 回滚方案

还原本分支提交即可。
