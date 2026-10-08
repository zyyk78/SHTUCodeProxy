# SHTUCodeProxy

自托管 HTTP 代理，提供 Anthropic Messages / OpenAI Responses / Chat Completions 转发，并支持模块化外部路由插件。

## 主要功能

- Anthropic Messages → OpenAI Responses / Chat Completions
- OpenAI Responses → upstream
- SSE / 非流式桥接
- 统一模型配置：`name` + `upstream_model`
- 外部插件路由（module + paths + handle）

## 快速开始

```bash
cp config.example.json src/config.json
PYTHONPATH=src python src/proxy.py
```

默认端口读取 `config.json` 中的 `port`，也可用 `PORT` / `HOST` 覆盖。

## 插件

在 `config.json` 中添加：

```json
{
  "plugin_dir": "/home/zyyk78/Downloads/SHTUCodeProxy/src/plugins",
  "plugins": [
    {"module": "comfy_workflow.py", "enabled": true, "timeout": 600}
  ]
}
```

插件模块必须定义：

```python
def routes():
    return [Route]
```

Route 需要：

```python
class Route:
    method = "GET"          # 或 "POST" / ("GET", "POST")
    paths = ("/comfy/workflow/health",)
    def handle(self, handler, config, plugin) -> bool: ...
```

路由顺序：

- `IP` 白/黑名单最先执行
- `GET/HEAD` 插件默认在鉴权后执行；`auth_exempt = true` 的健康检查可在鉴权前执行
- `POST/DELETE` 插件在鉴权后执行
- 内置 `/`、`/health`、`/v1/models`、`/v1/messages`、`/v1/responses` 保持不变
- 插件加载失败会直接阻止启动

## ComfyUI Workflow 插件

本仓库内置 `src/plugins/comfy_workflow.py`，通过 SHTUCodeProxy 暴露 ComfyUI workflow 任务接口。

**当前后端：ComfyUI（`scripts/comfy-run.py`）。** 文生图通过 ComfyUI `/prompt` API 提交 workflow；旧 diffusers 直连版本（`scripts/run.sh` → `generate.py`）已弃用，仅保留给编辑模式兜底。`comfy-run.py` 要求 ComfyUI 已在 `127.0.0.1:8188` 运行（可用 `COMFY_URL` 覆盖），可用 `QWEN_COMFY_UNET` / `QWEN_COMFY_CLIP` / `QWEN_COMFY_VAE` 覆盖模型文件名。

插件支持 **多 workflow 通用调用**：在 `workflows_dir`（options 里配置，默认 `/mnt/HDD1/llm/comfy/workflows`）下放 ComfyUI 前端导出的 UI 格式 `*.json`，agent 先查列表、再取可编辑面（surface）、最后提交修改后的 graph。

默认路径：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/comfy/workflow/health` | 插件状态 |
| GET | `/comfy/workflow/workflows` | **列出可用 workflow**（agent 第一步） |
| GET | `/comfy/workflow/workflows/<id>/surface` | **获取可编辑面**（agent 第二步：顶层参数/连线/图输入） |
| GET | `/comfy/workflow/graph/<id>` | 获取 workflow 原文（调试/编辑用） |
| POST | `/comfy/workflow/graph/submit` | **提交修改后的 graph**（agent 第三步，可带图） |
| GET | `/comfy/workflow/jobs?job_id=...` | 查询单个任务 |
| GET | `/comfy/workflow/jobs` | 查询 running/queued/history |
| GET | `/comfy/workflow/result?job_id=...` | 下载 PNG |
| DELETE | `/comfy/workflow/jobs/<job_id>` | 删除已完成任务 |

## 测试

```bash
PYTHONPATH=src uv run --with pytest --python /opt/anaconda3/bin/python3 pytest -q
```

## 配置

不要提交真实 API key。`src/config.json` 是本机配置；示例见 `config.example.json`。

插件部署相关值（`comfy_url` / `workflows_dir` / `data_root` / 各种上限）都放在 `plugins[].options` 里，不要硬编码进插件源码。完整 options 示例见 `config.example.json`。
