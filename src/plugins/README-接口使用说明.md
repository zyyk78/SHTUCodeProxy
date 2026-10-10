# ComfyUI Workflow 接口使用说明

> 适用范围：SHTUCodeProxy 的 ComfyUI Workflow 插件（`src/plugins/comfy_workflow.py`）。
> 后端：ComfyUI 0.38.0（部署在服务器本地，仅监听 `127.0.0.1:8188`，由 SHTUCodeProxy 插件对外转发）。
> 本文所有示例的 `BASE` / `AUTH` 替换成你的实际值。
>
> **agent 工作流（surface-first）**：
> 1. `GET /comfy/workflow/workflows` 列模板 →
> 2. `GET /comfy/workflow/workflows/<file>/surface` 拿顶层可编辑面 →
> 3. 读 `notes` 理解工作流功能和参数含义（官方说明），只改 `params` / `image_inputs` / `connections` →
> 4. `POST /comfy/workflow/graph/submit` 提交改好的 workflow JSON。
>
> **不要直接改 subgraph 内部节点**：已知模板的 subgraph 定义在提交时会被
> `canonical_subgraphs()`（扫描 `workflows_dir` 全部模板）覆盖锁定，agent 的
> 内部改动不会生效。surface 暴露的顶层参数（如 t2i 模板的 459
> `widgets_values_named.prompt` / `steps` / `seed`）才是 agent 的编辑面。
> 新模型模板放进 `workflows_dir` 即可被 surface 自动识别（不写死模型类型）。

## 0. 准备

### 0.1 连接信息

```bash
BASE=http://SERVER_IP:8095           # SHTUCodeProxy 地址：SERVER_IP 换成服务器实际 IP（HTTPS 实例用 https:// + -k）
KEY='你的auth_key'                    # config.json 里的 auth_key
AUTH="Authorization: Bearer $KEY"
```

### 0.2 准备图片

接口通过 **multipart/form-data 文件字段**上传图片（不是 base64）：

| 限制 | 规则 |
|---|---|
| 格式 | 仅 `.png` / `.jpg` / `.jpeg` / `.webp`（按后缀检查） |
| 大小 | 单任务所有文件总和 ≤ `upload_max_mb`（默认 50MB） |
| 数量 | 可重复多个文件字段；顺序会被如实回显 |

准备示例：

```bash
# 确认图片格式和大小
file input.png            # PNG image data, 1536 x 1536
du -h input.png           # 大小要 < 50MB

# 如果图太大，先压一下（示例：最长边 1536，质量 90）
/opt/anaconda3/bin/python3 - <<'PY'
from PIL import Image
img = Image.open("input.png")
img.thumbnail((1536, 1536))
img.save("input-small.png", quality=90)
PY
```

注意：图片内容本身不做 magic-number 校验，但改成 `.png` 后缀的非图片文件会在 ComfyUI 执行时报错并回传到 `/jobs`。

**agent 自举提示**：你在读的这份文档本身可以通过接口获取 —— `GET /comfy/workflow/docs`
返回全文（`?format=json` 返回端点清单摘要）。新会话不确定接口细节时，先调它，
不要依赖记忆或猜测路由。

## 1. 查询模板列表

```bash
curl -sk -H "$AUTH" "$BASE/comfy/workflow/workflows"
```

返回磁盘上 `workflows_dir`（`/mnt/HDD1/llm/comfy/workflows`）里**真实存在的** `.json` 文件：

```json
{
  "workflows": [
    {
      "file": "image_qwen_image_2_1_t2i.json",
      "description": null
    },
    {
      "file": "image_qwen_image_2_1_image_edit.json",
      "description": null
    },
    {
      "file": "image_qwen_image_2_1_background_removal.json",
      "description": null
    }
  ]
}
```

- `file`：磁盘上的实际文件名，也是后续取模板用的 id（带不带 `.json` 都行）
- `description`：取值顺序 —— ① workflow JSON 顶层 `"description"` 字段（显式声明优先）；② 模板自带 MarkdownNote/Note 的用法说明摘要（优先取标题含 "Usage" 的 note，跳过导航链接行，180 字符截断）。两者都没有才是 `null`。**agent 选模板时看这一步就够了**，不必逐个拉 surface
- 非法 JSON 文件会列出并标 `"error": "invalid json"`
- 每次请求都重新扫盘，新保存的文件下次请求立即可见，无需重启

健康检查（不需要 key）：

```bash
curl -sk "$BASE/comfy/workflow/health"
```

## 2. 阅读模板工作流（surface-first）

### 2.1 取模板原文

```bash
# 取完整 workflow 文件原文（逐字节和磁盘一致，无包装层）
curl -sk -H "$AUTH" "$BASE/comfy/workflow/graph/image_qwen_image_2_1_t2i" > template-t2i.json
```

404 说明文件名不对，用第 1 步返回的确切 `file` 名。

### 2.2 看懂结构（UI 格式 workflow）

当前保存的模板是 **UI 格式**（ComfyUI 前端导出），顶层是：

```jsonc
{
  "nodes": [...],          // 顶层简化节点（通常是 subgraph 引用）
  "links": [...],
  "definitions": {
    "subgraphs": [ { "nodes": [...], "links": [...] } ]   // 真正的节点图在这里
  },
  "version": "0.4"
}
```

**注意：这是调试视角。agent 正常编辑不要碰这里**——subgraph 内部节点
（KSampler、模型加载器等）在提交时会被 canonical 定义锁定，改了不生效。
正常编辑流程见 2.3-2.5（surface → 顶层 widgets_values_named → 提交）。

调试时重点在 `definitions.subgraphs[0].nodes`，每个节点：

```jsonc
{
  "id": 458,
  "type": "KSampler",       // 节点类型
  "title": "...",           // 可选标题
  "widgets_values": [...],  // ★ 可编辑参数值（按 UI 控件顺序排列）
  "inputs": [...], "outputs": [...], "properties": {...}
}
```

### 2.3 可编辑面（surface 端点，推荐）

**不要直接猜 subgraph 内部结构**。先调 surface 端点，接口会把"agent 只能编辑的顶层参数"确定性地列出来：

```bash
curl -sk -H "$AUTH" "$BASE/comfy/workflow/workflows/image_qwen_image_2_1_t2i/surface" | jq '{kind, params, connections, image_inputs}'
```

返回示例（t2i 模板）：

```jsonc
{
  "kind": "subgraph",       // "subgraph" = 有封装层；"flat" = 无封装层
  "params": [
    { "slot": 0, "name": "prompt", "value": "...", "declared_type": "STRING" },
    { "slot": 6, "name": "steps", "value": 25, "declared_type": "INT" },
    { "slot": 11, "name": "seed", "value": 447606998181262, "declared_type": "INT" },
    { "node": "13", "name": "13.aspect_ratio", "value": "1:1 (Square)", "declared_type": "COMBO" },
    { "node": "461", "name": "461.filename_prefix", "value": "Qwen_image_2.1", "declared_type": "STRING" },
    { "node": "461", "name": "461.format.bit_depth", "value": "8-bit", "declared_type": "COMBO" }
    // ... 共 23 个参数
  ],
  "connections": [
    // 顶层连线：ResolutionSelector → width/height 槽（这类连线不是参数，是结构）
    { "slot": 7, "name": "width", "from": { "node": "13", "slot": 0, "type": "ResolutionSelector" } }
  ],
  "image_inputs": [],        // 参考图槽位（t2i 没有图输入，编辑/背景去除模板会有）
  "notes": [                 // 模板自带的官方说明（MarkdownNote 原文）
    { "node": "468", "title": "Note: Usage", "text": "## Parameters\n- cfg: keep 1 ..." }
  ]
}
```

- `params`：**这些是 agent 能改的全部参数**。名字带 `slot` 的是封装模块的输入槽（改 UI workflow 里对应实例节点的 `widgets_values_named`），带 `node` 前缀的是普通顶层节点控件
- `connections`：顶层连线，说明了哪些槽位是连线输入（如 width/height 来自 ResolutionSelector，不能直接当参数改）
- `image_inputs`：参考图槽位；编辑/背景去除模板在这里列出必填/可选图输入
- `notes`：模板里 `MarkdownNote`/`Note` 节点的原文（`title` + `text`）。这是**官方对这个工作流的说明**——尺寸建议、`cfg`/`steps` 该设多少、`prompt` 怎么写、模型文件在哪。**判断参数含义时优先看 notes，不要靠参数名猜**。内容原样透传不改写；模板没写 note 就是空数组
- **subgraph 内部结构（KSampler、模型加载器等）不在 surface 里，提交时会被 canonical 定义锁定，改了也不生效**——这是刻意设计：agent 只改最上层

> **只改 `widgets_values_named`，不要改 `widgets_values`。** UI 导出的 workflow 同时存了两份
> 控件值：`widgets_values_named`（按控件名，如 `{"image": "a.png"}`）和 `widgets_values`
> （位置数组，如 `["a.png", "image"]`）。插件读的是 `widgets_values_named`，
> 改位置数组不会生效——surface 报的值也来自 `widgets_values_named`，两边一致。

### 2.4 调试方法：只读 subgraph 内部（不要在这里编辑）

用 jq **只读**每个 workflow 里有哪些内部节点和值（理解结构用）：

```bash
# 节点清单（id + 类型）
jq -r '.definitions.subgraphs[0].nodes[] | "\(.id)\t\(.type)\t\(.title // "")"' template-t2i.json

# KSampler 当前值（只读参考；编辑请走 2.5 的顶层 widgets_values_named）
jq '.definitions.subgraphs[0].nodes[] | select(.type=="KSampler") | {id, widgets_values}' template-t2i.json

# UNETLoader（模型文件 + 权重精度，只读参考）
jq '.definitions.subgraphs[0].nodes[] | select(.type=="UNETLoader") | {id, widgets_values}' template-t2i.json

# 文本编码（提示词在顶层实例 widgets_values_named.prompt，这里只看内部接线）
jq '.definitions.subgraphs[0].nodes[] | select(.type=="TextEncodeQwenImage21" or .type=="PrimitiveStringMultiline") | {id, type, widgets_values}' template-t2i.json

# 尺寸（EmptyLatentImage 的 width/height/batch，只读参考）
jq '.definitions.subgraphs[0].nodes[] | select(.type=="EmptyLatentImage") | {id, widgets_values}' template-t2i.json

# KV 缓存（QwenImage21Cache：显存换速度，只读参考）
jq '.definitions.subgraphs[0].nodes[] | select(.type=="QwenImage21Cache") | {id, widgets_values}' template-t2i.json
```

各模板的可编辑要点：

| 模板 | 常编辑参数 |
|---|---|
| `image_qwen_image_2_1_t2i` | 顶层 459 `widgets_values_named`：`prompt`（提示词）、`steps`/`cfg`/`seed`（采样）、`negative_prompt`、`switch`（prompt enhancer 开关）+ 顶层 13 `aspect_ratio`（尺寸）+ 顶层 461 `filename_prefix`/`format` |
| `image_qwen_image_2_1_image_edit` | 同上 + 顶层 LoadImage `image`（参考图文件名） |
| `image_qwen_image_2_1_background_removal` | 同上 + 输入图（surface `image_inputs` 列出必填/可选槽位） |

参考取值（4090 24GB，顶层 widgets_values_named 字段）：

- seed：0 – 4294967295
- steps：官方 50；CFG 4.0（官方）；2.1 也可 CFG 1.0
- 尺寸：256–2752 且为 16 的倍数；原生支持 2048×2048（通过 13.aspect_ratio/13.megapixels 控制）
- KV 缓存 dtype：`default`（无损）/ `int8`（省一半，误差≈bf16）/ `int4`（更省，误差大）——在顶层 widgets_values_named 里改，不要碰内部节点

### 2.5 编辑模板（改 surface 暴露的顶层参数）

**正确做法：只改顶层实例节点的 `widgets_values_named`**。KSampler、模型加载器都在 subgraph 内部，改它们无效（提交时被 canonical 覆盖）。

```bash
cp template-t2i.json modified-t2i.json

# 例 1：改顶层 459（封装模块实例）的 prompt 和 steps
#      （459 是 surface 里 slot 0/6 对应的顶层实例节点 id）
jq '(.nodes[] | select(.id==459) | .widgets_values_named.prompt) = "a red apple on a white table"
    | (.nodes[] | select(.id==459) | .widgets_values_named.steps) = 30
    | (.nodes[] | select(.id==459) | .widgets_values_named.seed) = 777' modified-t2i.json

# 例 2：改顶层尺寸（ResolutionSelector 13 的 aspect_ratio）
jq '(.nodes[] | select(.id==13) | .widgets_values_named.aspect_ratio) = "16:9 (Widescreen)"' modified-t2i.json

# 例 3：改参考图文件名（编辑模板；LoadImage 是顶层节点，直接改 .image）
#      disk_name 从提交响应的 uploaded_files[].disk_name 拿
jq '(.nodes[] | select(.type=="LoadImage") | .widgets_values_named.image) = "8fbca12582e84b3fa3c42cb621fca90e.png"' modified-edit.json
```

改完自检（JSON 合法 + 顶层参数确实变了）：

```bash
jq -e . modified-t2i.json >/dev/null && echo OK
jq '.nodes[] | select(.id==459) | .widgets_values_named.prompt, .widgets_values_named.steps, .widgets_values_named.seed' modified-t2i.json
```

## 3. 提交任务（graph 模式）

### 3.1 不带图（文生图）

```bash
curl -sk -X POST "$BASE/comfy/workflow/graph/submit" \
  -H "$AUTH" \
  --form-string "graph=$(jq -c . modified-t2i.json)"
```

**注意用 `--form-string` 而不是 `-F`**：workflow JSON 里含分号/等号时，curl 的 `-F` 值解析器会截断内容导致 JSON 不完整。`--form-string` 按字面处理整个值。

### 3.2 带参考图（图生图 / 编辑 / 背景去除）

```bash
RESP=$(curl -sk -X POST "$BASE/comfy/workflow/graph/submit" \
  -H "$AUTH" \
  --form-string "graph=$(jq -c . modified-t2i.json)" \
  -F 'image=@input-small.png;filename=my-photo.png')
echo "$RESP" | jq .
```

### 3.3 202 响应（重点看 uploaded_files）

```json
{
  "job_id": "783860eafe0e4f4dab3c673d11bba3be",
  "status": "queued",
  "mode": "graph",
  "uploaded_files": [
    {
      "field": "image",
      "original_name": "my-photo.png",
      "disk_name": "8fbca12582e84b3fa3c42cb621fca90e.png"
    }
  ],
  "cleanup": null
}
```

- `uploaded_files[].disk_name`：复制进 ComfyUI `input/` 后的文件名。`original_name` ↔ `disk_name` 一一对应

### 3.3.1 disk_name 可本地预计算（推荐一轮提交）

`disk_name` 就是 `sha256(文件内容)` + 后缀，**不需要先提交一次去拿**：

```bash
DISK="$(sha256sum my-photo.png | cut -d' ' -f1).png"
```

把 `$DISK` 填进模板顶层 `LoadImage` 的 `image`，然后**在同一次请求里带上文件**：

```bash
curl -sk -X POST "$BASE/comfy/workflow/graph/submit" \
  -H "$AUTH" \
  --form-string "graph=$(jq -c . modified.json)" \
  -F 'image=@my-photo.png;filename=my-photo.png'
```

> **不要用两轮提交**（先提交一次拿 disk_name，再提交一次引用它）。每个 job 结束时会
> 立即删掉它自己上传的临时文件，第一单跑完后第二单引用的文件已经不存在了，
> ComfyUI 会报 `Invalid image file`。一轮提交可以彻底避开这个竞态。

> 本地能算哈希的前提是文件内容不变。改过字节就必须重算，否则名字对不上。
- 提交失败的常见 400：`invalid graph JSON` / `too many nodes`（>200）/ `graph too large`（>256KB）/ `references missing node` / `contains unsafe path` / `no SaveImage output node` —— `detail` 字段带具体 node/key

### 3.4 文件生命周期（自动，无需手动管理）

| 阶段 | 行为 |
|---|---|
| 上传 | 原图落盘 `uploads/<uuid>.<后缀>`，同时复制进 ComfyUI `input/` 用 `disk_name` 引用 |
| 任务结束（成功/失败） | **立即删除** `uploads/` 与 ComfyUI `input/` 里的临时文件 |
| 结果图 | 插件收到后存 `outputs/<job_id>.png`（+ 同名 `.json` 元数据） |
| 结果清理 | `outputs/` PNG **满 50 张**触发惰性清理，删最旧的到 40 张（连带 JSON）；仍可 `DELETE /jobs/<id>` 手动删 |

## 4. 轮询等待

```bash
JOB=<job_id>

# 每 2-3 秒查一次（不要叠加轮询）
curl -sk -H "$AUTH" "$BASE/comfy/workflow/jobs?job_id=$JOB" | jq '{status, error, elapsed_sec}'
```

状态机：

| status | 含义 | 下一步 |
|---|---|---|
| `queued` | 排队中 | 继续等 |
| `running` | ComfyUI 执行中 | 继续等 |
| `completed` | 成功，`output_path` 可用 | 去下载 |
| `failed` | 失败，`error` 字段有原因 | 读报错 |

失败排查路径：

```bash
# 1) 202 阶段 400 → 请求根本没排队，看 detail 修 graph
# 2) ComfyUI 校验失败（如模型名不在列表）→ error 里带节点级信息
# 3) 执行失败 → 读任务日志
cat "/tmp/comfy-wf-data/logs/$JOB.log"   # 测试配置；线上是 data_root/logs/<job_id>.log
```

常用超时：渲染 50 步 2K 图约 1-2 分钟；`max_wait` 默认 600 秒。

## 5. 下载结果图

```bash
curl -sk -H "$AUTH" -o result.png \
  "$BASE/comfy/workflow/result?job_id=$JOB"

# 确认
file result.png    # PNG image data, 1024 x 1024
```

- 404：输出不存在（已过期清理或任务失败）
- 400：job_id 非法（含 `/` `\` `.`）

## 6. 手动删除单个任务

```bash
curl -sk -X DELETE -H "$AUTH" "$BASE/comfy/workflow/jobs/$JOB"
```

- `200 {"deleted": [...]}`：已删（png + json + uploads 残留）
- `409`：任务在排队/运行中，不能删
- `404`：找不到

## 7. 完整示例（一段跑通文生图）

```bash
BASE=https://SERVER_IP:8091          # SERVER_IP 换成服务器实际 IP
KEY='你的auth_key'
AUTH="Authorization: Bearer $KEY"

# 1. 模板列表
curl -sk -H "$AUTH" "$BASE/comfy/workflow/workflows" | jq .

# 2. 取 t2i 模板
curl -sk -H "$AUTH" "$BASE/comfy/workflow/graph/image_qwen_image_2_1_t2i" > template.json

# 3. 改 seed/steps/提示词
jq '(.nodes[] | select(.id==459) | .widgets_values_named.prompt) = "a red apple on a white table"
    | (.nodes[] | select(.id==459) | .widgets_values_named.steps) = 30
    | (.nodes[] | select(.id==459) | .widgets_values_named.seed) = 777' template.json > modified.json

# 4. 提交
RESP=$(curl -sk -X POST "$BASE/comfy/workflow/graph/submit" \
  -H "$AUTH" --form-string "graph=$(jq -c . modified.json)")
echo "$RESP" | jq .
JOB=$(echo "$RESP" | jq -r '.job_id')

# 5. 轮询到 completed
while true; do
  S=$(curl -sk -H "$AUTH" "$BASE/comfy/workflow/jobs?job_id=$JOB" | jq -r .status)
  echo "$S"
  [ "$S" = "completed" ] || [ "$S" = "failed" ] && break
  sleep 3
done

# 6. 下载
curl -sk -H "$AUTH" -o result.png "$BASE/comfy/workflow/result?job_id=$JOB"
file result.png
```

## 8. 速查

| 需要什么 | 接口 |
|---|---|
| 可用模板 | `GET /comfy/workflow/workflows` |
| **可编辑面（推荐先调）** | `GET /comfy/workflow/workflows/<file>/surface` |
| 模板原文 | `GET /comfy/workflow/graph/<file>` |
| 提交（改好的 graph，可带图） | `POST /comfy/workflow/graph/submit` |
| 查任务 | `GET /comfy/workflow/jobs?job_id=` |
| 查队列 | `GET /comfy/workflow/jobs`（无参数） |
| 下载结果 | `GET /comfy/workflow/result?job_id=` |
| 删除任务 | `DELETE /comfy/workflow/jobs/<job_id>` |
| 插件/后端状态 | `GET /comfy/workflow/health`（免 key） |
| **接口文档全文** | `GET /comfy/workflow/docs`（`?format=json` 返回端点清单摘要；agent 自查用，无需本地保存） |
| **焚毁结果** | `POST /comfy/workflow/purge`（body: `{"job_id":"..."}` 或 `{"all":true}`；删除插件产物副本并清任务记录，不可恢复） |

## 产物存放在哪里

一次生成会产生**两份文件**：

| 位置 | 内容 | 谁管理 |
|---|---|---|
| ComfyUI `output/<managed_output_subdir>/<日期>_<前缀>_*.png` | ComfyUI 落盘的原件 | ComfyUI 写入；插件提交时改写 `filename_prefix` 把它路由进子目录 |
| 插件 `data_root/outputs/<job_id>.png` + `.json` | 插件复制的副本 + 元数据 | 插件；`DELETE /jobs/<id>` 和 `POST /purge` 都只清这份 |

**与 Web 端隔离**：配置 `managed_output_subdir`（如 `api-web`）后，插件提交的
任务会被自动改写 `filename_prefix` 为 `<subdir>/<日期>_原名`，ComfyUI 原生
把 prefix 的目录部分当子文件夹，于是插件的图全部落到
`output/api-web/20261010_Qwen_image_2.1_00001.png` 这类路径，Web 端手工
生成的图仍在 output 根目录 —— 两边物理隔离，互不干扰。

- 子目录不存在时 ComfyUI 自动创建
- 日期标签自动带上（`%Y%m%d`），按天归档
- 恶意 graph 里的绝对路径 filename_prefix 会被 basename 化后归入子目录，无法逃逸
- `managed_output_subdir` 不配置则不改写（兼容旧行为，图落到 output 根目录）

purge/DELETE 清的是**副本**；原件在 managed 子目录里按日期滚动，需要彻底
清理时删 `comfy_output_root/<managed_output_subdir>/` 下的文件即可。

## VRAM 空闲看门狗

ComfyUI 会把模型常驻显存（单卡可到 ~13GB）加速连续生成；共享服务器上长时间不生成时这是白占。插件内置空闲看门狗，三个条件同时满足才卸载：

1. 队列空闲（没有正在跑/排队的任务）
2. 空闲时长 ≥ `vram_unload_idle_s`（秒，默认 0 = 关闭）
3. 任一卡占用 ≥ `vram_unload_used_gb`（GB，默认 8）

触发时插件调 ComfyUI `POST /free` 挂上卸载标志。**注意语义**：`/free` 不会立即卸载，标志是持久的（sticky），真正的卸载发生在「下一个任务跑完之后」。因此插件做了配套防护：

- 触发后如果**有新任务到来**，插件会立即撤销标志（再次 `POST /free` 设 false 覆盖），任务正常执行，模型保持常驻 —— 连续生成不受影响
- 任务结束时若标志仍在（即确实无人回来用），ComfyUI 在任务收尾时统一卸载，下次生成重新加载（首次慢几十秒）

在 `config.json` 的 `plugins[].options` 里配置：

```json
"vram_unload_idle_s": 1800,
"vram_unload_used_gb": 8
```

上例表示：空闲 30 分钟且某卡占用 ≥8GB 时自动卸载。设 `vram_unload_idle_s: 0` 关闭看门狗。

## 换模型与加 LoRA

模型文件目录（`extra_model_paths.yaml` 指向的 `models/` 树）由 ComfyUI **自动发现**——新 `.safetensors` 丢进对应目录后，loader 节点的下拉选项立即包含它，无需重启。但**自动发现 ≠ 自动应用**：workflow 里 `UNETLoader.unet_name` 等引用是写死的，不改编译不换模型。

**换模型**（两种等价方式）：

1. 改模板：在 ComfyUI 里打开模板 → 换 loader 的模型名 → 重新导出到 `workflows_dir`，之后所有调用都用新模型
2. 提交时换：surface 里 `unet_name` / `clip_name` / `vae_name` 是 COMBO 参数，提交前在 graph 里改成新文件名即可 —— 同一模板可跑不同模型

**加 LoRA**：`models/loras/` 丢入 `.safetensors` 后，任何带 `LoraLoader` 节点的模板即可在 surface 里直接编辑 `lora_name` / `strength_model` / `strength_clip`。当前内置的三个 Qwen-Image 模板原生不带 LoRA 节点——需要的话在 ComfyUI 里加一个 `LoraLoader`（model/clip 串在 UNetLoader/CLIPLoader 之后）再导出模板即可，`workflows_dir` 放进去就会被 surface 自动识别，不需要改插件代码。

**多卡说明**：ComfyUI 是单设备调度（`get_torch_device()` 固定 `cuda:0`），不会自动均衡双卡。要利用第二张卡，跑第二个 ComfyUI 实例（`CUDA_VISIBLE_DEVICES=1`，不同端口）并在插件里按需路由，是最小改动路径。

## options 配置项汇总

| 键 | 默认 | 说明 |
|---|---|---|
| `comfy_url` | `http://127.0.0.1:8188` | ComfyUI 地址（仅本机回环，由插件对外转发） |
| `workflows_dir` | （必填） | 模板目录，新模板放入即被 surface 识别 |
| `comfy_output_root` | （必填） | ComfyUI 的 output 目录，插件从这里取产物 |
| `comfy_input_dir` | （可选） | ComfyUI 的 input 目录，上传图复制到这里 |
| `data_root` | `~/.local/share/comfy-workflow-api` | 插件副本/日志根目录（**建议放持久盘，`/tmp` 重启即清空**） |
| `managed_output_subdir` | （空 = 关闭） | 插件产物隔离子目录，见「产物存放在哪里」 |
| `prompt_max` | 8000 | prompt 长度上限 |
| `upload_max_mb` | 50 | 单任务上传总量上限 |
| `workflow_max_kb` / `workflow_max_nodes` | 256 / 200 | 提交 graph 的大小/节点数上限 |
| `max_wait` | 600 | 单任务最长等待秒数 |
| `output_retention_hours` / `output_max_files` | 24 / 200 | 副本生命周期（惰性清理） |
| `vram_unload_idle_s` / `vram_unload_used_gb` | 0（关）/ 8 | VRAM 看门狗，见上文 |
| `cleanup_interval_s` | 300 | 清理线程间隔

## 写给全新 Agent：五分钟上手

你是第一次接入本服务的 AI Agent？按下面的顺序做，不需要任何背景知识：

**第 1 步：拿到连接信息。** 问你的使用者要三样东西：`BASE`（本代理地址）、
`KEY`（config.json 里的 auth_key）。`managed_output_subdir` 是服务端配置，
提交时无需关心。

**第 2 步：取文档。** `GET $BASE/comfy/workflow/docs` 拿到本手册全文；要程序
化消费就用 `?format=json` 拿端点清单。文档是权威依据，之后的任何不确定都以
它为准，不要靠猜。

**第 3 步：看有哪些模板。** `GET /comfy/workflow/workflows`，`description`
字段直接告诉你每个模板是干嘛的。选定模板后：

**第 4 步：取可编辑面。** `GET /comfy/workflow/workflows/<file>/surface` ——
`params` 是你能改的参数（prompt/seed/尺寸/模型名等），`image_inputs` 是要传
图的槽位，`notes` 是模板作者写的官方说明。**只改 surface 暴露的东西**，
subgraph 内部改了也不会生效。

**第 5 步：改参数并提交。** `GET /comfy/workflow/graph/<file>` 拿原文 →
按 surface 的指引改值 → `POST /comfy/workflow/graph/submit`（要传图就用
multipart 的 `image` 字段，服务端返回 `disk_name`，填进对应 `image_inputs`）。

**第 6 步：轮询与取图。** `GET /comfy/workflow/jobs?job_id=...` 每 2-3 秒查
一次；`completed` 后 `GET /comfy/workflow/result?job_id=...` 下载 PNG。

常见操作速记：删任务 `DELETE /jobs/<id>`；彻底清产物 `POST /purge`；
服务端参数含义不确定时回第 2 步重读文档。

## Agent 出图任务指导：从需求到成图

这一节写给**接到具体出图需求**的 Agent（比如用户说「帮我生成一张赛博朋克
风格的城市夜景」）。按顺序执行，每一步都别跳：

### 第 1 步：理解需求，读基础说明

明确用户的真实意图：文生图还是改图？要什么风格/主体/氛围？有没有参考图？
然后通读本手册的 0-2 节（连接方式、参数限制、surface-first 编辑模型）。

### 第 2 步：查模板列表，决策用哪个模型

`GET /comfy/workflow/workflows`，逐个看 `description` 与模板名的对应关系：

- 从零生成 → t2i 模板
- 基于参考图改图/换装/去背景 → image_edit / background_removal 模板

**决策依据要充分**：不确定某个模板能否满足需求时，把候选模板的
`surface` 都拉下来对比（`image_inputs`、模型文件名、notes 里的说明）。
如果看完仍然判断不了哪个合适（比如用户的需求横跨两类模板），**直接问
用户**，不要替用户赌一个。也可以用 web_search 查一下需求里的术语
（如 "Qwen-Image 编辑能力"、"RGBA 透明输出"）再回来决策。

### 第 3 步：仔细阅读所选模板的说明与笔记

拉取选定模板的 surface，**完整阅读 `notes` 数组**——这是模板作者写的官方
说明，包含关键信息：

- **模型能力边界**：某些模型/节点支持 RGBA 透明通道输出（t2i 模板的
  "Transparent Image" 章节）、某些只支持特定分辨率倍数（如 32 的倍数）
- **输入资产要求**：image_edit 模板对参考图的格式/内容有明确说明
- **参数含义**：switch（prompt enhancer 开关）、KV 缓存等选项的效果

跳过这一步是大多数失败提交的根源。

### 第 4 步：编辑流程，重点打磨提示词

按 surface 暴露的 `params` 修改，**提示词是成图质量的第一决定因素**：

- **描述精确、具体、有层次**：主体是什么、穿什么、在什么环境、什么光线、
  什么镜头/构图。避免「一张好看的图」这种空泛表述
- **善用 negative_prompt**：把不想要的元素明确排除
- **I2I 修图模型**：按 notes 说明对原图做标注——有的支持在图上圈选区域、
  有的要求用绘图工具做 Mask（白=改/黑=保留，具体看模板 notes），把「改哪里」
  传达给模型，而不是指望它自己猜
- 拿不准措辞时可以 web_search 该模型社区的常用提示词写法（如
  "Qwen-Image prompt examples"），或找符合需求的参考图描述作为骨架
- 需要透明背景时记得查 notes 里的 RGBA 相关开关

### 第 5 步：提交并轮询

`GET /graph/<file>` → 按 surface 改值 → `POST /graph/submit`（带图需求用
multipart 上传，把响应的 `disk_name` 填进 image_inputs）→ 每 2-3 秒
`GET /jobs?job_id=` 轮询。

### 第 6 步：报错处理

`failed` 时**读完整错误信息再动手**：

- ComfyUI 的 `node_errors` 会精确指出哪个节点、什么原因（模型名不存在、
  图片格式不合法、参数越界等）——按提示改对应字段后重新提交
- `Invalid image file` → 通常是 disk_name 填错或图片格式/后缀不合规
- 参数校验类报错（prompt 超长、尺寸非倍数）→ 改完再提交
- 同一错误连续两次修不好 → 停下来向用户说明，不要无限重试

### 质量自检（可选但推荐）

成图后可以自行检查是否符合需求（透明背景是否真的透明、参考图元素是否
保留）。不满意时**改参数重跑**比向用户道歉更有价值——seed 换一个、
prompt 加细节、steps 提高，都是有效的迭代手段。

## 把本服务封装成 Codex Skill

如果你在 Codex / Claude Code 这类支持 Skills 的环境里长期使用本服务，建议把
上面的流程沉淀成一个 Skill，让任何新会话零配置上手：

**目录结构**（放在 `~/.codex/skills/comfy-image/` 或项目 `.codex/skills/`）：

```
comfy-image/
└── SKILL.md          # 唯一必需文件
```

**SKILL.md 模板**（复制后把 `BASE`/`KEY` 换成实际值）：

```markdown
---
name: comfy-image
description: 通过 SHTUCodeProxy 的 ComfyUI Workflow 插件生成/编辑图片。
  当用户要求文生图、图生图、背景去除，或提到 ComfyUI/Qwen-Image 时使用。
---

# ComfyUI 图片生成

## 连接
- BASE: https://服务器IP:8090   (自签证书加 curl -k)
- AUTH: Authorization: Bearer <auth_key>

## 标准流程 (surface-first, 严格按序)
1. GET  $BASE/comfy/workflow/workflows                  # 列模板, 看 description 选
2. GET  $BASE/comfy/workflow/workflows/<file>/surface   # 拿可编辑面
3. 只改 surface 的 params/image_inputs; subgraph 内部改了无效
4. GET  $BASE/comfy/workflow/graph/<file>               # 拿原文
5. POST $BASE/comfy/workflow/graph/submit               # multipart: graph 字段
   (要传图: 追加 image 字段, 用响应里的 disk_name 填 LoadImage)
6. GET  $BASE/comfy/workflow/jobs?job_id=...            # 2-3 秒一次
7. GET  $BASE/comfy/workflow/result?job_id=...          # completed 后下载

## 硬性规则
- 不确定接口细节时先 GET /comfy/workflow/docs 重读文档, 不猜路由
- 编辑只走 surface; 已知模板的 subgraph 会被服务端锁定覆盖
- prompt 上限 8000 字符; 单任务上传 ≤50MB
- 任务结束后 uploads 自动清理; 产物副本在 data_root/outputs/
```

**要点**：`description` 字段决定 Agent 何时自动触发这个 Skill，要写清楚触发
场景；流程写「标准路径」即可，异常处理让 Agent 现场查 docs。装好后新会话
直接说「帮我生成一张图」就会走这套流程，无需再贴任何文档。
