# SHTUCodeProxy 使用说明书

---

## 目录

- [项目简介](#项目简介)
- [快速开始](#快速开始)
- [配置详解](#配置详解)
  - [基础配置](#基础配置)
  - [安全配置](#安全配置)
  - [模型配置](#模型配置)
- [安全功能](#安全功能)
  - [密钥认证](#密钥认证)
  - [IP 黑白名单](#ip-黑白名单)
  - [HTTPS 加密](#https-加密)
- [命令行参数](#命令行参数)
- [API 端点](#api-端点)
- [常见场景](#常见场景)
- [故障排除](#故障排除)
- [项目结构](#项目结构)

---

## 项目简介

SHTUCodeProxy 是一个轻量级 AI 模型代理服务器，专为 **Claude Code** 桌面端设计。它的核心功能是：

- 🔄 **协议转换**：将 Anthropic Messages API 格式转换为 OpenAI Responses / Chat Completions 格式，使 Claude Code 可以对接非 Anthropic 上游模型
- 🌊 **流式输出**：完整支持 SSE（Server-Sent Events）流式传输，实时返回模型响应
- 🧠 **多模型路由**：支持同时配置多个上游模型，自动按 `model_id` 路由
- 🔐 **安全防护**：密钥认证 + IP 黑白名单 + 可选 HTTPS，三重安全防线
- ⚡ **性能优化**：orjson 加速、单遍状态机、增量更新、SSE 批量写入

**支持的 API 格式：**

| 格式 | 上游示例 | 说明 |
|------|----------|------|
| `responses` | OpenAI Responses API | 默认格式 |
| `chat_completions` | OpenAI Chat Completions API | 兼容 DeepSeek、GLM 等 |

---

## 快速开始

### 环境要求

- Python 3.8+
- 可选：`orjson`（性能提升 2-10 倍，`pip install orjson`）

### 启动代理

```bash
# 方式一：直接运行
python3 src/proxy_refine.py

# 方式二：指定监听地址
python3 src/proxy_refine.py --host 0.0.0.0 --port 8090
```

### 配置 Claude Code

1. 打开 Claude Code Desktop 设置
2. 将 API Base URL 设为 `http://127.0.0.1:8090`
3. 将 API Key 设为 `config.json` 中的 `auth_key` 值
4. 选择模型（下拉菜单会显示配置的所有模型）

---

## 配置详解

配置文件位于 `src/config.json`，修改后**重启代理**生效。

### 基础配置

```json
{
  "host": "127.0.0.1",
  "port": 8090,
  "timeout": 30000,
  "default_stream": true,
  "log_level": 0
}
```

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `host` | string | `"127.0.0.1"` | 监听地址。`127.0.0.1` 仅本机，`0.0.0.0` 允许局域网访问 |
| `port` | int | `8082` | 监听端口 |
| `timeout` | int | `300` | 上游请求超时时间（毫秒） |
| `default_stream` | bool | `true` | 默认使用流式输出 |
| `log_level` | int | `-1` | 日志级别。`-1` = 不启用（不写日志文件）；`0` = 静默；`1` = 仅错误；`2` = 信息（默认）；`3` = 详细。`-1` 时回退环境变量 `SHTU_LOG_LEVEL`，未设则默认 `2` |

### 安全配置

```json
{
  "auth_key": "666laotie",
  "ssl_cert": "",
  "ssl_key": "",
  "allowed_ips": [],
  "denied_ips": []
}
```

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `auth_key` | string | `""` | 访问密钥。为空则不验证。支持 `Bearer` 和 `x-api-key` 两种传递方式 |
| `ssl_cert` | string | `""` | SSL 证书文件路径。为空则使用 HTTP |
| `ssl_key` | string | `""` | SSL 私钥文件路径。为空则使用 HTTP |
| `allowed_ips` | array | `[]` | IP 白名单。为空则不限制。支持单 IP 和 CIDR |
| `denied_ips` | array | `[]` | IP 黑名单。优先级高于白名单。支持单 IP 和 CIDR |

### 模型配置

```json
{
  "models": [
    {
      "name": "deepseek-pro",
      "model_id": "deepseek-pro",
      "base_url": "https://genaiapi.shanghaitech.edu.cn/api/v1/start",
      "api_key": "your-api-key",
      "upstream_model": "deepseek-pro",
      "api_format": "chat_completions",
      "supports_image": false,
      "supports_audio": false,
      "supports_video": false,
      "max_context_tokens": "128000"
    }
  ]
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 否 | 模型显示名称，默认同 `model_id` |
| `model_id` | string | 是 | 模型唯一标识符，用于路由请求 |
| `base_url` | string | 否 | 上游 API 地址 |
| `api_key` | string | 否 | 上游 API 密钥 |
| `upstream_model` | string | 否 | 发送给上游的实际模型名，默认同 `model_id` |
| `api_format` | string | 否 | API 格式：`"responses"` 或 `"chat_completions"` |
| `supports_image` | bool | 否 | 是否支持图片输入 |
| `supports_audio` | bool | 否 | 是否支持音频输入 |
| `supports_video` | bool | 否 | 是否支持视频输入 |
| `max_context_tokens` | string | 否 | 最大上下文 token 数 |
| `supports_reasoning` | bool | 否 | 是否支持推理/思考模式 |
| `enable_thinking` | bool | 否 | 是否向上游发送 `enable_thinking: true`（vLLM 模型） |
| `stream_bridge` | bool | 否 | 是否启用流式桥接（qwen 模型默认开启） |

---

## 安全功能

### 密钥认证

当 `auth_key` 非空时，所有请求必须携带正确的密钥：

**方式一：Authorization Header**
```bash
curl -H "Authorization: Bearer 666laotie" http://127.0.0.1:8090/health
```

**方式二：x-api-key Header**
```bash
curl -H "x-api-key: 666laotie" http://127.0.0.1:8090/health
```

> ⚠️ 密钥为空时任何人都能访问，建议始终设置。

### IP 黑白名单

支持两种模式，**可同时使用，黑名单优先级更高**：

#### 白名单模式 — 仅允许指定 IP

```json
{
  "allowed_ips": ["127.0.0.1", "192.168.1.0/24"],
  "denied_ips": []
}
```

- 只有 `127.0.0.1` 和 `192.168.1.*` 网段的客户端可以访问
- 其他 IP 返回 `403 Forbidden`

#### 黑名单模式 — 封禁指定 IP

```json
{
  "allowed_ips": [],
  "denied_ips": ["192.158.5.1", "10.0.0.0/8"]
}
```

- `192.158.5.1` 和 `10.*.*.*` 网段被禁止访问
- 其他所有 IP 放行

#### 混合模式 — 白名单 + 黑名单

```json
{
  "allowed_ips": ["192.168.1.0/24"],
  "denied_ips": ["192.168.1.100"]
}
```

- 仅 `192.168.1.*` 网段可访问（白名单）
- 但 `192.168.1.100` 被明确封禁（黑名单优先）

#### CIDR 语法速查

| 写法 | 含义 | 匹配范围 |
|------|------|---------|
| `127.0.0.1` | 单个 IP | 仅本机 |
| `192.168.1.0/24` | C 类网段 | 192.168.1.0 ~ 192.168.1.255 |
| `10.0.0.0/16` | B 类网段 | 10.0.0.0 ~ 10.0.255.255 |
| `172.16.0.0/12` | 跨网段 | 172.16.0.0 ~ 172.31.255.255 |
| `0.0.0.0/0` | 所有 IPv4 | 等同于白名单不限制 |

#### 检查顺序

```
请求进入
  ↓
① 黑名单检查 → 命中则 403（最高优先级）
  ↓
② 白名单检查 → 非空且不命中则 403
  ↓
③ 放行（继续密钥认证）
```

### HTTPS 加密

当 `ssl_cert` 和 `ssl_key` 均非空且文件有效时，代理自动启用 HTTPS。

#### 生成自签证书

```bash
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout key.pem -out cert.pem -days 365 \
  -subj "/CN=localhost"
```

#### 配置

```json
{
  "ssl_cert": "/path/to/cert.pem",
  "ssl_key": "/path/to/key.pem"
}
```

#### 启动效果

```
[2025-06-12 20:00:00] Listening on https://0.0.0.0:8090
[2025-06-12 20:00:00] IP blacklist: disabled
[2025-06-12 20:00:00] IP whitelist: disabled (allow all)
[2025-06-12 20:00:00] Configured models: deepseek-pro, glm-chat, MiniMax-M2.7
```

> ⚠️ 自签证书客户端会报不信任警告。Claude Code 需设置环境变量 `NODE_TLS_REJECT_UNAUTHORIZED=0`（仅限开发环境）。生产环境建议使用 Nginx/Caddy 反向代理 + Let's Encrypt 正式证书。

#### 推荐生产部署方式

```
客户端 ──[HTTPS]──> Nginx/Caddy (443) ──[HTTP]──> SHTUCodeProxy (127.0.0.1:8090)
```

代理只需监听 `127.0.0.1`，由 Nginx/Caddy 处理 TLS 终止，无需配置 `ssl_cert`/`ssl_key`。

---

## 命令行参数

```
python3 src/proxy_refine.py [选项]
```

| 参数 | 环境变量 | 默认值 | 说明 |
|------|----------|--------|------|
| `--host` | `HOST` | config.json 中的值 | 监听地址 |
| `--port` | `PORT` | config.json 中的值 | 监听端口 |

示例：

```bash
# 监听所有网卡
python3 src/proxy_refine.py --host 0.0.0.0 --port 8090

# 通过环境变量指定
HOST=0.0.0.0 PORT=8090 python3 src/proxy_refine.py
```

---

## API 端点

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| GET | `/` | 是 | 健康检查 |
| GET | `/health` | 是 | 健康检查 |
| GET | `/v1` | 是 | 健康检查 |
| GET | `/v1/models` | 是 | 获取可用模型列表 |
| POST | `/v1/messages` | 是 | Anthropic Messages API 代理 |
| POST | `/v1/responses` | 是 | OpenAI Responses API 代理 |
| OPTIONS | 任意 | 否 | CORS 预检 |

### 健康检查示例

```bash
curl -H "Authorization: Bearer 666laotie" http://127.0.0.1:8090/health
# 返回: {"ok": true, "service": "shtu-claude-proxy"}
```

### 模型列表示例

```bash
curl -H "Authorization: Bearer 666laotie" http://127.0.0.1:8090/v1/models
```

```json
{
  "object": "list",
  "data": [
    {"id": "deepseek-pro", "object": "model", "owned_by": "shtu-proxy", "max_context_tokens": 128000},
    {"id": "glm-chat", "object": "model", "owned_by": "shtu-proxy", "max_context_tokens": 200000},
    {"id": "MiniMax-M2.7", "object": "model", "owned_by": "shtu-proxy", "max_context_tokens": 200000}
  ]
}
```

---

## 常见场景

### 场景一：本机开发（最简配置）

```json
{
  "host": "127.0.0.1",
  "port": 8090,
  "auth_key": "",
  "allowed_ips": [],
  "denied_ips": [],
  "ssl_cert": "",
  "ssl_key": ""
}
```

> 仅本机访问，无密钥，无限制。适合本地开发调试。

### 场景二：局域网共享

```json
{
  "host": "0.0.0.0",
  "port": 8090,
  "auth_key": "my-secret-key",
  "allowed_ips": ["192.168.1.0/24"],
  "denied_ips": [],
  "ssl_cert": "",
  "ssl_key": ""
}
```

> 局域网内可访问，需密钥，仅允许 `192.168.1.*` 网段。

### 场景三：公网部署（最安全）

```json
{
  "host": "127.0.0.1",
  "port": 8090,
  "auth_key": "strong-random-key-here",
  "allowed_ips": [],
  "denied_ips": ["已知恶意IP"],
  "ssl_cert": "",
  "ssl_key": ""
}
```

> 代理仅监听本机，由 Nginx/Caddy 反代处理 HTTPS。密钥 + 黑名单双重防护。

### 场景四：封禁特定 IP

```json
{
  "host": "0.0.0.0",
  "port": 8090,
  "auth_key": "my-secret-key",
  "allowed_ips": [],
  "denied_ips": ["192.158.5.1", "10.0.0.0/8"],
  "ssl_cert": "",
  "ssl_key": ""
}
```

> 允许所有 IP 访问，但封禁特定 IP 或网段。

---

## 故障排除

### 启动报错：`SSL cert file not found`

**原因**：`ssl_cert` 指向的证书文件不存在或路径错误。

**解决**：
1. 检查文件路径是否正确（建议使用绝对路径）
2. 确认文件权限：`ls -la /path/to/cert.pem`
3. 如不需要 HTTPS，将 `ssl_cert` 和 `ssl_key` 置空

### 启动报错：`Failed to load SSL cert/key`

**原因**：证书/私钥文件损坏或格式不匹配。

**解决**：
1. 确认是 PEM 格式：`openssl x509 -in cert.pem -text -noout`
2. 确认私钥匹配证书：`openssl rsa -in key.pem -check`
3. 重新生成证书

### 客户端报 `403 Forbidden`

**原因**：客户端 IP 被黑白名单拦截。

**解决**：
1. 查看代理日志，确认拦截原因：
   - `ip denied: x.x.x.x in blacklist` → 在 `denied_ips` 中
   - `ip rejected: x.x.x.x not in whitelist` → 不在 `allowed_ips` 中
2. 调整配置或添加客户端 IP 到白名单

### 客户端报 `401 Unauthorized`

**原因**：密钥错误或未提供。

**解决**：
1. 确认请求头包含 `Authorization: Bearer <auth_key>` 或 `x-api-key: <auth_key>`
2. 确认 `auth_key` 值与配置一致

### 流式输出中断 / 长对话卡死

**原因**：上游超时或 `max_tokens` 超出上下文限制。

**解决**：
1. 增大 `timeout` 值（默认 30000ms）
2. 检查 `max_context_tokens` 是否与模型实际限制匹配
3. 代理会自动裁剪超出上下文窗口的 `max_tokens`

### Claude Code 显示 "Gateway returned no usable models"

**原因**：Claude Code Desktop 过滤模型名称，拒绝非 Anthropic 命名的模型。

**解决**：代理已内置 Claude 别名机制（`claude-sonnet-4`、`claude-opus-4` 等），会自动映射到配置的上游模型。确保 `model_env` 字段正确配置。

---

## 项目结构

```
SHTUCodeProxy-main/
├── README.md              # 本说明书
├── VERSION                # 版本号
├── .gitignore
└── src/
    ├── config.json        # 配置文件
    ├── proxy_refine.py    # 主程序（HTTP 服务器 + 协议转换）
    ├── config_store.py    # 配置解析与存储
    ├── platform_utils.py  # 跨平台工具函数
    ├── safe_io.py         # 安全文件 IO / 进程锁
    └── proxy.log          # 运行日志（自动生成）
```

---

## 性能优化说明

`proxy_refine.py` 采用以下优化策略：

| 优化项 | 说明 |
|--------|------|
| orjson | 优先使用 orjson（2-10x faster），回退标准库 json |
| 单遍状态机 | 替代多遍扫描解析 SSE 事件 |
| 增量更新 | 替代全量重建响应对象 |
| OrderedDict | 保持插入序，避免排序开销 |
| SSE 批量写入 | 6-10 个事件合并为 1 次 write/flush |
| 延迟 visible_text | 按需生成，避免无用计算 |
| 浅层 copy | 替代 deepcopy，减少内存分配 |
| join 替 += | 字符串拼接优化 |

---

## 许可证

内部项目，仅供授权使用。
