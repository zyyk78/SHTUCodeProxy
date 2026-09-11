# SHTUCodeProxy

一个轻量级的 **Anthropic ↔ OpenAI 协议代理服务器**，让 Claude Code / Codex CLI 可以对接非 Anthropic 的上游模型（上海科技大学 genaiapi 网关、MiniMax 等）。

本项目 fork 自 `SaberJack` 的原始项目，作为独立分支维护。主要改动：拆分重构后端代码、新增**透传模式**（直接对接原生 Anthropic / OpenAI Responses 上游）、补充安全配置、日志分级、性能优化与若干 bug 修复。

> 目前仅维护后端（`src/`）。前端 GUI 与打包脚本已移除，请直接改 `config.json` 后运行 `proxy.py`。

---

## 核心能力

申请到上科大无限量大模型的API了吗？

**好，把他们上市！**

本项目可以把你申请到的API接入Claude Code、CodeX

⚠注意！本项目是SaberJack同名项目的Fork,但是因为两边需求和代码库差别越来越大，所以本分支将会单独维护

核心功能如下：

- 转换（网关）模式，将你的编程工具接入上科大的 LLM API，适用于上科大这种一个模型一个key的零散场景
- 透传模式，可以自由在校内模型和你自己买的模型之间切换（类似于CC-Switch）

---

## 快速开始

### 环境要求

- Python 3.10+
- 可选 `orjson`（`pip install orjson`）

### 准备配置

把config.example.json复制到src文件夹，并改名为config.json

编辑 `src/config.json`的models字段

- **host**：默认`127.0.0.1`即可

- **port**：开通访问的端口，接入时会练到这里

- **timeout**：上游请求的超时时间（秒）

- **default_stream**：默认开启流式输出

- **auth_key**：就是你自定义下游接入所需的key

- **log_level**：日志等级，0-3分别为silent、error、info、debug

- **ssl_cert**&**ssl_key**：用于开启SSL,填写后自动开启HTTPS访问并关闭HTTP，如果仅本地部署本地使用倒是无所谓，但是请配合下面的白名单

- **allowed_ips**&**denied_ips**：黑白名单，黑名单高于白名单，支持单 IP 与 CIDR，如果仅本地部署使用，请在白名单里面写一个127.0.0.1（会自动开启）

在example中的models字段，前两个是转换模式

- **name**是你自定义的下游用来请求的名字，你可以随便起，但不能重名

- **upstream_model**是向上游请求的模型名称（申请后会告诉你）

- **base_url**地址都是`https://genaiapi.shanghaitech.edu.cn/api/v1/start`

- **api_key**申请了会给你

- **api_format**应该都是`chat_completions`（后续不知道会不会有变化，不过申请到的ChatGPT是response，我没测试过这个玩意）

- **support xxx**是三个能力项，根据模型自行判断

- **max_context_tokens**用于广播当前模型的参数，没啥用

- **supports_reasoning**&**enable_thinking**基本都支持，填true即可

后两个是透传模式，会直接转发请求到目标网址，唯一的区别就是会将请求模型中的地址从**name**替换为**upstream_model**，也就是替换为你真正要请求的模型名称，不过claudecode和codex请赋予不同的名称且指向对应的真实请求地址

*写完后如果程序启动不起来，就检查一下json格式有没有错*

### 启动

用python运行proxy.py，也可以自行打包

linux系用户可以注册为systemd，参考src/shtu-proxy.service实现自动启动

windows用户可以自己注册为服务

### API 端点

| 方法   | 路径                          | 说明                      |
| ---- | --------------------------- | ----------------------- |
| GET  | `/`、`/health`、`/v1`         | 健康检查                    |
| GET  | `/v1/models`、`/models`      | 可用模型列表                  |
| POST | `/v1/messages`              | Anthropic Messages 代理入口 |
| POST | `/v1/responses`             | OpenAI Responses 代理入口   |
| POST | `/v1/messages/count_tokens` | Token 计数                |

---

## 客户端接入

具体配置方法可以参考各模型商的官方接入方式

下面一些简单说明：

### Claude Code

1. API Base URL 填 `http://主机IP:port`，如果开启了SSL就要改为HTTPS，默认就是
2. API Key 填 `config.json` 的 `auth_key`为你写的那个auth_key
3. 其他参数自行配置

### Codex CLI

在 `~/.codex/config.toml` 指向本代理，`wire_api` 用 `responses`，模型名填 `config.json` 里的 `model_id`。

然后在~/.codex/auth.json里填入key
