"""ComfyUI workflow route plugin for SHTUCodeProxy.

ComfyUI 接管所有模型和推理；本插件只负责 workflow 管理、参数校验和任务队列。
模型权重的加载/卸载由 ComfyUI 自动管理（内部 RAM-pressure cache）。

    "plugins": [
      {
        "module": "comfy_workflow.py",
        "enabled": true,
        "timeout": 600,
        "options": {
          "comfy_url": "http://127.0.0.1:8188",
          "workflows_dir": "/path/to/comfy/workflows",
          "data_root": "/path/to/comfy-workflow-data",
          "prompt_max": 8000,
          "upload_max_mb": 50
        }
      }
    ]
"""
from __future__ import annotations

import json
import copy
import hashlib
import os
import queue
import shutil
import urllib.error
import urllib.request
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote, urlparse

from plugin_manager import RoutePlugin


class Settings:
    """插件的部署相关配置。全部可由 config.json / 环境变量提供。"""

    def __init__(self, options: Optional[dict] = None):
        opts = options if isinstance(options, dict) else {}
        self.comfy_url: str = str(_first_set(opts.get("comfy_url"), os.environ.get("COMFY_URL")) or "http://127.0.0.1:8188").rstrip("/")
        data_root = _first_set(opts.get("data_root"), os.environ.get("COMFY_WORKFLOW_DATA_ROOT"))
        # data_root 未配置时落到用户态目录（不写死任何用户名）。
        self.data_root: Path = Path(
            data_root if data_root else Path.home() / ".local" / "share" / "comfy-workflow-api"
        ).expanduser().resolve()
        self.prompt_max = _to_int(opts.get("prompt_max"), os.environ.get("COMFY_WORKFLOW_PROMPT_MAX"), 8000)
        self.upload_max_bytes = _to_int(opts.get("upload_max_mb"), os.environ.get("COMFY_WORKFLOW_UPLOAD_MAX_MB"), 50) * 1024 * 1024
        wf = _first_set(opts.get("workflows_dir"), os.environ.get("COMFY_WORKFLOW_WORKFLOWS_DIR"))
        self._workflows_dir: Optional[Path] = Path(wf).expanduser().resolve() if wf else None
        self.model_name = str(opts.get("model_name") or "ComfyUI")
        out_root = _first_set(opts.get("comfy_output_root"), os.environ.get("COMFY_OUT_ROOT"))
        self.output_root: Optional[Path] = Path(out_root).expanduser().resolve() if out_root else None
        self.max_wait = _to_int(opts.get("max_wait"), os.environ.get("COMFY_WORKFLOW_MAX_WAIT"), 600)
        in_dir = _first_set(opts.get("comfy_input_dir"), os.environ.get("COMFY_INPUT_DIR"))
        self.comfy_input_dir: Optional[Path] = Path(in_dir).expanduser().resolve() if in_dir else None
        # workflow 模式限制（防止超大/恶意 workflow 与存储爆炸）
        self.workflow_max_bytes = _to_int(opts.get("workflow_max_kb"), os.environ.get("COMFY_WORKFLOW_MAX_KB"), 256) * 1024
        self.workflow_max_nodes = _to_int(opts.get("workflow_max_nodes"), os.environ.get("COMFY_WORKFLOW_MAX_NODES"), 200)
        # 输出生命周期
        self.output_retention_hours = _to_int(opts.get("output_retention_hours"), os.environ.get("COMFY_WORKFLOW_OUTPUT_RETENTION_HOURS"), 24)
        self.output_max_files = _to_int(opts.get("output_max_files"), os.environ.get("COMFY_WORKFLOW_OUTPUT_MAX_FILES"), 200)
        self.cleanup_interval_s = _to_int(opts.get("cleanup_interval_s"), os.environ.get("COMFY_WORKFLOW_CLEANUP_INTERVAL_S"), 300)

    @property
    def output_dir(self) -> Path:
        return self.data_root / "outputs"

    @property
    def upload_dir(self) -> Path:
        return self.data_root / "uploads"

    @property
    def log_dir(self) -> Path:
        return self.data_root / "logs"

    @property
    def workflows_dir(self) -> Optional[Path]:
        return self._workflows_dir

    def unconfigured(self) -> list:
        """列出缺失的必填项（空列表 = 配置完整）。"""
        missing = []
        if not self.comfy_url:
            missing.append("comfy_url")
        if not self._workflows_dir or not self._workflows_dir.is_dir():
            missing.append("workflows_dir")
        if not self.output_root:
            missing.append("comfy_output_root")
        return missing

    def ready(self) -> bool:
        return not self.unconfigured()


def _first_set(*values) -> Optional[str]:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _to_int(value, env_value, default: int) -> int:
    for candidate in (value, env_value):
        if candidate is None:
            continue
        try:
            return int(str(candidate).strip())
        except (TypeError, ValueError):
            continue
    return default


def _to_bool(value, env_value, default: bool) -> bool:
    for candidate in (value, env_value):
        if candidate is None:
            continue
        if isinstance(candidate, bool):
            return candidate
        text = str(candidate).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return default


SETTINGS = Settings()

# 下面三个名字保留给内部逻辑（Worker / 各 handler）使用, 指向 SETTINGS。
DATA_ROOT = SETTINGS.data_root
OUTPUT_DIR = SETTINGS.output_dir
UPLOAD_DIR = SETTINGS.upload_dir
LOG_DIR = SETTINGS.log_dir
PROMPT_MAX = SETTINGS.prompt_max
UPLOAD_MAX = SETTINGS.upload_max_bytes
WORKFLOWS_DIR = SETTINGS.workflows_dir


def configure(options: Optional[dict] = None) -> None:
    """由 plugin_manager 在加载时调用一次（routes() 之前）。"""
    global SETTINGS, DATA_ROOT, OUTPUT_DIR, UPLOAD_DIR, LOG_DIR
    global PROMPT_MAX, UPLOAD_MAX, WORKFLOWS_DIR
    SETTINGS = Settings(options)
    DATA_ROOT = SETTINGS.data_root
    OUTPUT_DIR = SETTINGS.output_dir
    UPLOAD_DIR = SETTINGS.upload_dir
    LOG_DIR = SETTINGS.log_dir
    PROMPT_MAX = SETTINGS.prompt_max
    UPLOAD_MAX = SETTINGS.upload_max_bytes
    WORKFLOWS_DIR = SETTINGS.workflows_dir


class Job:
    def __init__(self, job_id: str, form: dict, files: list[Path], workflow: Optional[dict] = None, params: Optional[dict] = None, graph: Optional[dict] = None):
        self.id = job_id
        self.form = form
        self.files = files
        self.workflow = workflow or {}
        self.params = params or {}
        self.graph = graph or {}
        self.status = "queued"
        self.error = None
        self.output = None
        self.log = None
        self.returncode = None
        self.created = datetime.now().isoformat(timespec="seconds")
        self.started = None
        self.finished = None
        self.elapsed = None

    def public(self):
        return {
            "job_id": self.id, "status": self.status, "error": self.error,
            "created": self.created, "started": self.started,
            "finished": self.finished, "elapsed_sec": self.elapsed,
            "output_path": str(self.output) if self.output else None,
            "log_path": str(self.log) if self.log else None,
            "returncode": self.returncode,
            "workflow": self.workflow.get("name") or self.workflow.get("workflow") or None,
            "params": self.params if self.params else None,
            "mode": "graph" if self.graph else ("params" if self.workflow else None),
        }


class Worker:
    def __init__(self, start: bool = False):
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.current = None
        self.history: dict[str, Job] = {}
        self.thread: Optional[threading.Thread] = None
        if start:
            self.start_once()

    def start_once(self):
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self.run, daemon=True, name="comfy-workflow-worker")
            self.thread.start()

    def run(self):
        while True:
            job = self.q.get()
            with self.lock:
                self.history[job.id] = job
                self.current = job
                job.status = "running"
                job.started = datetime.now().isoformat(timespec="seconds")
            t0 = time.monotonic()
            try:
                if not SETTINGS.ready():
                    raise RuntimeError(
                        "comfy workflow 未正确配置: 请在 config.json 的 plugins[].options 里设置 "
                        "comfy_url 与 workflows_dir (缺失项: %s)" % (SETTINGS.unconfigured() or ["unknown"])
                    )
                f = job.form
                job_id_hex = job.id
                out_path = OUTPUT_DIR / f"{job_id_hex}.png"
                if job.graph:
                    # graph 模式: agent 已经修改好完整 workflow，直接提交。
                    # WHY: agent 通过 202 响应的 uploaded_files 拿到 disk_name 后自己
                    # 填进 LoadImage；这里只负责把上传文件复制到 ComfyUI input/。
                    graph = copy.deepcopy(job.graph)
                    input_dir = getattr(SETTINGS, "comfy_input_dir", None)
                    if input_dir and input_dir.is_dir() and job.files:
                        for up in job.files:
                            shutil.copy2(up, input_dir / up.name)
                else:
                    raise RuntimeError("job has no graph (graph 模式必填)")

                comfy_url = SETTINGS.comfy_url
                # 记录提交时刻，用于防止 ComfyUI 写错目录后插件拿到旧的同名输出。
                submitted_ts = time.time()
                req = urllib.request.Request(
                    f"{comfy_url}/prompt",
                    data=json.dumps({"prompt": graph}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=30) as r:
                        submit = json.loads(r.read())
                except urllib.error.HTTPError as e:
                    # WHY: ComfyUI 的校验错误全在响应 body 里（节点级信息），
                    # urllib 在抛 HTTPError 时会丢掉 body，agent 就只能看到
                    # "HTTP Error 400: Bad Request" 这种没法定位的提示。
                    # WHY: 补上 body 里的 ComfyUI 错误结构，agent 才能知道是哪个节点。
                    body = ""
                    try:
                        body = e.read().decode("utf-8", "replace")
                    except Exception:
                        pass
                    raise RuntimeError(
                        f"ComfyUI rejected prompt (HTTP {e.code}): {body[:2000]}"
                    ) from e
                prompt_id = submit.get("prompt_id", "")
                if not prompt_id:
                    raise RuntimeError(f"ComfyUI did not return prompt_id: {submit}")

                # poll history
                entry = None
                deadline = time.monotonic() + getattr(SETTINGS, "max_wait", 600)
                while time.monotonic() < deadline:
                    try:
                        with urllib.request.urlopen(f"{comfy_url}/history/{prompt_id}", timeout=10) as r:
                            hist = json.loads(r.read())
                    except Exception:
                        time.sleep(2); continue
                    entry = hist.get(prompt_id)
                    if entry is None:
                        time.sleep(2); continue
                    status = str(entry.get("status", {}).get("status_str", "")).lower()
                    if status == "error":
                        raise RuntimeError(f"ComfyUI workflow failed: {json.dumps(entry.get('status', {}), ensure_ascii=False)}")
                    if status == "success" or entry.get("outputs"):
                        break
                    time.sleep(2)
                else:
                    raise RuntimeError(f"ComfyUI timeout waiting for {prompt_id}")

                # find output image and copy
                rel = None
                for node_out in (entry or {}).get("outputs", {}).values():
                    if "images" in node_out:
                        img = node_out["images"][0]
                        rel = os.path.join(img.get("subfolder", ""), img["filename"])
                        break
                if not rel:
                    raise RuntimeError("no output image in ComfyUI history")
                comfy_out_root = getattr(SETTINGS, "output_root", None)
                if not comfy_out_root:
                    raise RuntimeError("comfy_output_root not configured in plugins[].options")
                src = os.path.join(comfy_out_root, rel)
                if not os.path.isfile(src):
                    raise RuntimeError(f"output not found: {src}")
                src_mtime = os.path.getmtime(src)
                if src_mtime < submitted_ts:
                    raise RuntimeError(
                        "output predates task — path config may be wrong "
                        f"(output mtime={src_mtime:.3f}, submitted={submitted_ts:.3f})"
                    )
                shutil.copy2(src, out_path)
                job.output = out_path
                job.status = "completed"
                job.returncode = 0
            except Exception as e:
                job.status = "failed"; job.error = str(e)
                if job.returncode is None: job.returncode = -1
            finally:
                job.elapsed = round(time.monotonic() - t0, 1)
                job.finished = datetime.now().isoformat(timespec="seconds")
                # WHY: 上传文件是临时输入，任务结束（成功或失败）立即清理，
                # 防止 uploads 目录持续膨胀。
                for up in getattr(job, "files", []) or []:
                    try:
                        up.unlink(missing_ok=True)
                        comfy_in = getattr(SETTINGS, "comfy_input_dir", None)
                        if comfy_in and comfy_in.is_dir():
                            (comfy_in / up.name).unlink(missing_ok=True)
                    except Exception:
                        pass
                with self.lock: self.current = None


worker = Worker(start=bool(os.environ.get("COMFY_WORKFLOW_PLUGIN_AUTOSTART", "1") not in ("0", "false", "no", "off")))


def find_job(job_id):
    with worker.lock:
        job = worker.history.get(job_id)
    if job is None:
        return None
    if job.status == "queued" and job.id not in [j.id for j in list(worker.q.queue)]:
        worker.q.put(job)
    return job


def completed_info(job_id):
    output = OUTPUT_DIR / f"{job_id}.png"
    if not output.is_file(): return None
    out = {"job_id": job_id, "status": "completed", "output_path": str(output),
           "finished": datetime.fromtimestamp(output.stat().st_mtime).isoformat(timespec="seconds")}
    meta = OUTPUT_DIR / f"{job_id}.json"
    if meta.is_file():
        try:
            m=json.loads(meta.read_text())
            out.update({k:m.get(k) for k in ("elapsed_sec","seed","width","height")})
        except Exception: pass
    return out


def _wf_dir() -> Optional[Path]:
    return SETTINGS.workflows_dir


def list_workflows() -> list:
    """扫描 workflows_dir 里的实际 workflow 文件。

    只返回磁盘上真实存在的文件；description 只从 workflow JSON 自身的
    顶层 "description" 字段读取，没有就是 null，不做任何猜测/缓存。
    """
    d = _wf_dir()
    if not d or not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            # 非法 JSON 的文件也列出来，description 标记 parse error
            out.append({"file": p.name, "description": None, "error": "invalid json"})
            continue
        if isinstance(meta, dict):
            desc = meta.get("description")
            out.append({"file": p.name, "description": desc if isinstance(desc, str) else None})
        else:
            out.append({"file": p.name, "description": None})
    return out


def read_multipart(handler: BaseHTTPRequestHandler):
    from email.parser import BytesParser
    from email.policy import HTTP
    ctype = handler.headers.get("content-type", "")
    if not ctype.startswith("multipart/form-data"):
        raise ValueError("multipart/form-data required")
    raw = handler.rfile.read(int(handler.headers.get("content-length", "0") or "0"))
    msg = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + ctype.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw
    )
    fields, files = {}, []
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        if filename:
            data = part.get_payload(decode=True) or b""
            if data:
                files.append((name or "image", filename, data))
        elif name:
            fields[name] = part.get_payload(decode=True).decode("utf-8", "replace")
    return fields, files


def safe_output(job_id):
    if "/" in job_id or "\\" in job_id or job_id in ("", ".", ".."):
        raise ValueError("invalid job id")
    p = (OUTPUT_DIR / f"{job_id}.png").resolve()
    p.relative_to(OUTPUT_DIR.resolve())
    if not p.is_file(): raise FileNotFoundError("output not found")
    return p


def send_json(handler, status, payload):
    data = json.dumps(payload, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(data)


# ---------------- UI → API workflow conversion ----------------
#
# 架构约定（重要）: agent 只编辑 UI workflow 的"最上层"——
#   * subgraph 类模板: 顶层实例节点的 widgets_values_named / 顶层 LoadImage 等
#   * 普通 UI 模板: 顶层节点的 widgets_values
# subgraph 内部结构在提交时被已知模板的 canonical 定义覆盖锁定，
# agent 的内部改动不会生效；未知 subgraph id 才按提交内容透传（支持新模板）。

_SKIP_NODE_TYPES = {"MarkdownNote", "Note", "Reroute"}
_SKIP_WIDGET_NAMES = {"upload"}  # UI 专用上传按钮，API 不需要


def _is_ui_workflow(graph) -> bool:
    return isinstance(graph, dict) and isinstance(graph.get("nodes"), list)


def _link_index(links) -> dict:
    """UI links（顶层数组形式 / subgraph 对象形式）→ {id: (origin, oslot, target, tslot)}。"""
    index = {}
    for link in links or []:
        if isinstance(link, list) and len(link) >= 5:
            index[int(link[0])] = (link[1], link[2], link[3], link[4])
        elif isinstance(link, dict) and link.get("id") is not None:
            index[int(link["id"])] = (
                link.get("origin_id"), link.get("origin_slot"),
                link.get("target_id"), link.get("target_slot"),
            )
    return index


def _widget_input_names(ui_node: dict) -> list[str]:
    """按 UI 控件顺序返回 widget 输入名（widgets_values 下标 ↔ 输入名）。"""
    names = []
    for inp in ui_node.get("inputs") or []:
        if isinstance(inp, dict) and inp.get("widget") and inp.get("name"):
            # "upload" 是 LoadImage 的 UI 专用上传按钮，不是 API 输入
            if str(inp["name"]) == "upload":
                continue
            names.append(str(inp["name"]))
    return names


def _widgets_by_name(ui_node: dict) -> dict:
    """widgets_values 按控件名展开。

    seed/noise_seed 的 control_after_generate 在 UI 值里多占一位
    （fixed/randomize），API 只取值本身；已连接的控件同样占位但被 link 覆盖。
    """
    names = _widget_input_names(ui_node)
    values = ui_node.get("widgets_values")
    if not isinstance(values, list):
        return {}
    out = {}
    vi = 0
    for name in names:
        if vi >= len(values):
            break
        out[name] = values[vi]
        if name in ("seed", "noise_seed"):
            vi += 2  # 值 + control_after_generate 模式
        else:
            vi += 1
    return out


def _follow_origin(origin_id, origin_slot, link_index: dict, nodes_by_id: dict, depth: int = 0):
    """解析连线起点，穿透 Reroute 链到真实输出节点。

    origin_id 为 -10/-20（subgraph 虚拟输入/输出）时直接返回，
    由调用方按 subgraph 输入槽语义解析。
    """
    if depth > 8:
        raise ValueError("reroute chain too deep")
    if int(origin_id) in (-10, -20):
        return str(int(origin_id)), int(origin_slot or 0)
    node = nodes_by_id.get(str(origin_id))
    if node is None:
        raise ValueError(f"link origin node {origin_id} not found")
    if str(node.get("type")) != "Reroute":
        return str(origin_id), int(origin_slot or 0)
    inputs = node.get("inputs") or []
    if not inputs:
        raise ValueError("reroute node has no inputs")
    link_id = inputs[0].get("link")
    if link_id is None:
        raise ValueError("reroute node input link is empty")
    origin = link_index.get(int(link_id))
    if origin is None:
        raise ValueError(f"reroute link {link_id} not found")
    return _follow_origin(origin[0], origin[1], link_index, nodes_by_id, depth + 1)


def _subgraph_output_source(subgraph: dict, slot: int):
    """subgraph 输出槽 → 内部输出节点（通过 target_id=-20 的链接反查）。"""
    for link in subgraph.get("links") or []:
        if not isinstance(link, dict):
            continue
        if link.get("target_id") == -20 and int(link.get("target_slot") or 0) == slot:
            return str(link.get("origin_id")), int(link.get("origin_slot") or 0)
    raise ValueError(f"subgraph {subgraph.get('id')} output slot {slot} has no source link")


_object_info_cache: dict = {"ts": 0.0, "data": {}}
_OBJECT_INFO_TTL = 300.0


def _get_object_info() -> dict:
    """从 ComfyUI 拉 object_info（带 TTL 缓存），失败时返回上次缓存或空 dict。

    WHY: socketless 控件（如 ImageCompare.compare_view）在 UI 导出里
    widgets_values 为空，但 /prompt 又要求必填；用 object_info 的默认值
    通用补齐，而不是按节点类型写死。拉取失败时保持旧行为（不猜值）。
    """
    now = time.time()
    cached = _object_info_cache.get("data") or {}
    if cached and now - _object_info_cache.get("ts", 0.0) < _OBJECT_INFO_TTL:
        return cached
    try:
        with urllib.request.urlopen(f"{SETTINGS.comfy_url}/object_info", timeout=10) as r:
            data = json.loads(r.read())
        if isinstance(data, dict) and data:
            _object_info_cache["ts"] = now
            _object_info_cache["data"] = data
            return data
    except Exception:
        pass
    return cached


def _fill_widget_defaults(node_type: str, inputs: dict, ui_inputs: Optional[list]) -> None:
    """按 ComfyUI object_info 给未赋值的 widget 输入补默认值。

    规则（保守，只处理 UI 已声明为 widget 且无连线的输入）:
      - 已有值（来自连线或 widgets_values）→ 跳过
      - object_info options[0] 是字符串 → 直接用（socketless 控件，如 IMAGECOMPARE）
      - options[0] 是列表 → 用第一个选项（combo 默认值）
    没有命中规则的一律不猜值，交给 ComfyUI 校验报错。
    """
    info = _get_object_info().get(node_type)
    if not isinstance(info, dict):
        return
    required = ((info.get("input") or {}).get("required") or {})
    ui_by_name = {
        str(i.get("name")): i
        for i in ui_inputs or []
        if isinstance(i, dict) and i.get("name")
    }
    for name, spec in required.items():
        if name in inputs or not isinstance(spec, list) or not spec:
            continue
        ui_decl = ui_by_name.get(name)
        if not isinstance(ui_decl, dict) or not ui_decl.get("widget"):
            continue
        if ui_decl.get("link") is not None:
            continue
        choices = spec[0]
        if isinstance(choices, str):
            inputs[name] = choices
        elif isinstance(choices, list) and choices:
            inputs[name] = choices[0]


def _ref_input_values(ref_node: dict, subgraph: dict, link_index: dict) -> dict:
    """解析 subgraph 引用节点的输入: slot → 字面值 / API 引用。

    优先级: 顶层连线 > widgets_values_named > widgets_values 位置映射。
    没有值的槽（如未使用的 images.image_N）不进结果，转换时按可选输入省略。
    """
    named = ref_node.get("widgets_values_named")
    named = named if isinstance(named, dict) else {}
    positional = _widgets_by_name(ref_node)
    out = {}
    for slot, sgi in enumerate(subgraph.get("inputs") or []):
        if not isinstance(sgi, dict):
            continue
        name = str(sgi.get("name") or "")
        rn_inputs = ref_node.get("inputs") or []
        rn_input = rn_inputs[slot] if slot < len(rn_inputs) else None
        link_id = rn_input.get("link") if isinstance(rn_input, dict) else None
        if link_id is not None:
            origin = link_index.get(int(link_id))
            if origin is None:
                raise ValueError(f"ref node {ref_node.get('id')}.{name}: link {link_id} not found")
            out[slot] = [str(origin[0]), int(origin[1] or 0)]
        elif name in named:
            out[slot] = named[name]
        elif name in positional:
            out[slot] = positional[name]
    return out


def ui_workflow_to_api(ui_workflow: dict, canonical_subgraphs: Optional[dict] = None) -> dict:
    """把 ComfyUI UI 格式 workflow（nodes 数组 + subgraph 定义）转成 API 格式。

    canonical_subgraphs: 已知模板的 subgraph 定义（id → definition）。
    提供时覆盖提交内容里的同名 subgraph——锁定内部结构，agent 只能改顶层参数。
    """
    if not _is_ui_workflow(ui_workflow):
        raise ValueError("not a UI-format workflow (missing nodes array)")
    top_nodes = [n for n in ui_workflow.get("nodes") or [] if isinstance(n, dict)]
    if not top_nodes:
        raise ValueError("UI workflow has no nodes")

    defs = ui_workflow.get("definitions") or {}
    subgraph_by_id = {}
    for sg in defs.get("subgraphs") or []:
        if isinstance(sg, dict) and sg.get("id"):
            subgraph_by_id[str(sg["id"])] = sg
    if canonical_subgraphs:
        # WHY: 锁定已知模板内部结构——agent 只编辑顶层参数和顶层连线，
        # subgraph 内部节点（模型组合、采样器接线）不允许被提交内容改写。
        for sg_id, sg in canonical_subgraphs.items():
            subgraph_by_id[str(sg_id)] = sg

    api_nodes = {}

    def convert_container(container_nodes, container_links, parent_refs, parent_sg_id):
        link_index = _link_index(container_links)
        nodes_by_id = {str(n.get("id")): n for n in container_nodes if isinstance(n, dict)}
        for node in container_nodes:
            if not isinstance(node, dict):
                continue
            node_id = node.get("id")
            node_type = str(node.get("type") or "")
            if node_type in _SKIP_NODE_TYPES or not node_type:
                continue
            if node_type in subgraph_by_id:
                sg = subgraph_by_id[node_type]
                refs = _ref_input_values(node, sg, link_index)
                convert_container(sg.get("nodes") or [], sg.get("links"), refs, node_type)
                continue
            inputs = {}
            by_name = _widgets_by_name(node)
            named_vals = node.get("widgets_values_named")
            named_vals = named_vals if isinstance(named_vals, dict) else {}
            for inp in node.get("inputs") or []:
                if not isinstance(inp, dict):
                    continue
                name = str(inp.get("name") or "")
                if not name:
                    continue
                if name == "upload":
                    continue  # LoadImage 的 UI 上传按钮，不是 API 输入
                link_id = inp.get("link")
                if link_id is not None:
                    origin = link_index.get(int(link_id))
                    if origin is None:
                        raise ValueError(f"node {node_id}.{name}: link {link_id} not found")
                    origin_id, origin_slot = origin[0], int(origin[1] or 0)
                    if origin_id == -10 or origin_id == "-10":
                        # subgraph 内部链接: -10 = 顶层引用节点的参数输入。
                        # 先尝试穿透 Reroute（-10 链接不会经过 Reroute，但保持对称）。
                        if parent_refs is None:
                            raise ValueError(f"node {node_id}.{name}: subgraph input link outside subgraph")
                        slot = int(origin_slot)
                        if slot not in parent_refs:
                            continue  # 未使用的可选输入（如 images.image_N）省略
                        inputs[name] = parent_refs[slot]
                    elif origin_id == -20 or origin_id == "-20":
                        continue
                    else:
                        origin_node = nodes_by_id.get(str(origin_id))
                        if origin_node and str(origin_node.get("type")) == "Reroute":
                            # 穿透 Reroute 链到真实输出节点
                            rid, rslot = _follow_origin(origin_id, origin_slot, link_index, nodes_by_id)
                            origin_node = nodes_by_id.get(rid)
                            origin_id, origin_slot = rid, rslot
                        if origin_node and str(origin_node.get("type")) in subgraph_by_id:
                            # subgraph 引用节点的输出 → 内部真实输出节点
                            sg = subgraph_by_id[str(origin_node["type"])]
                            out_id, out_slot = _subgraph_output_source(sg, int(origin_slot))
                            inputs[name] = [out_id, out_slot]
                        else:
                            # WHY: ComfyUI /prompt 要求节点 id 一律为字符串；
                            # origin[0] 是 link_index 里的原始 int，必须转 str。
                            inputs[name] = [str(origin_id), int(origin_slot)]
                elif name in named_vals:
                    # WHY: UI 格式同时存在两种表示。widgets_values_named 是带控件名
                    # 的权威值，widgets_values 只是同值的位置数组。必须优先读 named，
                    # 否则 agent 按 surface 改 named 会被位置数组悄悄覆盖回去。
                    inputs[name] = named_vals[name]
                elif name in by_name:
                    inputs[name] = by_name[name]
            _fill_widget_defaults(node_type, inputs, node.get("inputs") or [])
            api_nodes[str(node_id)] = {"class_type": node_type, "inputs": inputs}

    convert_container(top_nodes, ui_workflow.get("links"), None, None)
    return api_nodes


def extract_editable_surface(ui_workflow: dict) -> dict:
    """提取 UI workflow 的顶层可编辑面（agent 只看/只改这一层）。

    subgraph 类: 参数来自实例节点 widgets_values_named + 顶层 LoadImage 等；
    连接列出顶层外部连线（如 ResolutionSelector → 尺寸、上传图 → 参考图槽）。
    notes: 模板自带的 MarkdownNote/Note 文本（官方说明、参数含义、模型清单），
    原样透传给 agent 作为"这个工作流是什么、参数怎么用"的权威依据。
    """
    if not _is_ui_workflow(ui_workflow):
        raise ValueError("not a UI-format workflow")
    top_nodes = [n for n in ui_workflow.get("nodes") or [] if isinstance(n, dict)]
    defs = ui_workflow.get("definitions") or {}
    subgraph_types = {
        str(sg.get("id"))
        for sg in defs.get("subgraphs") or []
        if isinstance(sg, dict) and sg.get("id")
    }
    top_links = _link_index(ui_workflow.get("links"))
    nodes_by_id = {str(n.get("id")): n for n in top_nodes}
    surface = {"kind": "flat", "params": [], "connections": [], "image_inputs": [], "notes": []}

    def collect_notes(container_nodes) -> None:
        for n in container_nodes or []:
            if not isinstance(n, dict):
                continue
            if str(n.get("type") or "") not in ("MarkdownNote", "Note"):
                continue
            values = n.get("widgets_values")
            text = values[0] if isinstance(values, list) and values else n.get("text")
            if isinstance(text, str) and text.strip():
                surface["notes"].append({
                    "node": str(n.get("id")),
                    "title": n.get("title") or None,
                    "text": text,
                })

    for node in top_nodes:
        node_id = str(node.get("id"))
        node_type = str(node.get("type") or "")
        if node_type in _SKIP_NODE_TYPES:
            continue
        if node_type in subgraph_types:
            surface["kind"] = "subgraph"
            named = node.get("widgets_values_named")
            named = named if isinstance(named, dict) else {}
            positional = _widgets_by_name(node)
            for slot, inp in enumerate(node.get("inputs") or []):
                if not isinstance(inp, dict):
                    continue
                name = str(inp.get("name") or "")
                if not name:
                    continue
                link_id = inp.get("link")
                if link_id is not None:
                    origin = top_links.get(int(link_id))
                    src = None
                    if origin:
                        src_node = nodes_by_id.get(str(origin[0]))
                        src = {
                            "node": str(origin[0]),
                            "slot": int(origin[1] or 0),
                            "type": str(src_node.get("type")) if src_node else None,
                        }
                    surface["connections"].append({
                        "slot": slot, "name": name, "node": node_id, "from": src,
                    })
                    continue
                value = named.get(name, positional.get(name))
                if str(inp.get("type")) == "IMAGE" or name.startswith("images."):
                    surface["image_inputs"].append({
                        "slot": slot, "name": name, "node": node_id, "value": value,
                    })
                else:
                    surface["params"].append({
                        "slot": slot, "name": name, "node": node_id, "value": value,
                        "declared_type": str(inp.get("type") or ""),
                    })
            continue
        # 顶层普通节点（LoadImage / SaveImageAdvanced / 自定义辅助节点）
        by_name = _widgets_by_name(node)
        named_vals = node.get("widgets_values_named")
        named_vals = named_vals if isinstance(named_vals, dict) else {}
        for inp in node.get("inputs") or []:
            if not isinstance(inp, dict):
                continue
            name = str(inp.get("name") or "")
            if not name:
                continue
            link_id = inp.get("link")
            if link_id is not None:
                continue  # 节点间连线不属于参数面
            if name in named_vals or name in by_name:
                # WHY: 报出来的值必须和提交时真正生效的值一致。转换层优先读
                # widgets_values_named，这里也优先读，否则 agent 看到 A 改完
                # 却跑出 B，会以为是自己改错了。
                value = named_vals.get(name, by_name.get(name))
                surface["params"].append({
                    "node": node_id, "name": f"{node_id}.{name}",
                    "value": value, "declared_type": str(inp.get("type") or ""),
                })
    # WHY: 子图内部也可能有 note（当前三个模板为 0，但换模板后可能有），
    # 同样透传给 agent，不做内容改写。
    for sg in defs.get("subgraphs") or []:
        if isinstance(sg, dict):
            collect_notes(sg.get("nodes"))
    collect_notes(top_nodes)
    return surface


def canonical_subgraphs() -> dict:
    """扫描 workflows_dir 全部模板，收集 {subgraph id: definition} 用于锁定内部结构。"""
    out = {}
    d = _wf_dir()
    if not d or not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            graph = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for sg in ((graph.get("definitions") or {}).get("subgraphs") or []) if isinstance(graph, dict) else []:
            if isinstance(sg, dict) and sg.get("id"):
                out[str(sg["id"])] = sg
    return out


def validate_workflow_graph(graph: dict, settings: "Settings") -> None:
    """提交前合规校验。失败抛 ValueError，由调用方转成 400 + 完整报错。

    校验内容:
      1. 结构: dict[str, {"class_type": str, "inputs": dict}]
      2. 节点数上限
      3. 引用完整性: ["<node>", <slot>] 必须指向存在的节点
      4. 不允许绝对路径 / .. 逃逸的文件引用
      5. 必须存在 SaveImage/PreviewImage 类输出节点
    """
    if not isinstance(graph, dict) or not graph:
        raise ValueError("workflow graph must be a non-empty object")
    if len(graph) > settings.workflow_max_nodes:
        raise ValueError(f"too many nodes: {len(graph)} > {settings.workflow_max_nodes}")

    has_output = False
    for node_id, node in graph.items():
        if not isinstance(node_id, str) or not node_id:
            raise ValueError(f"invalid node id: {node_id!r}")
        if not isinstance(node, dict):
            raise ValueError(f"node {node_id} must be an object")
        class_type = node.get("class_type")
        if not isinstance(class_type, str) or not class_type.strip():
            raise ValueError(f"node {node_id} missing class_type")
        if not isinstance(node.get("inputs", {}), dict):
            raise ValueError(f"node {node_id} inputs must be an object")
        if class_type.startswith(("SaveImage", "PreviewImage")):
            has_output = True
        for key, val in node.get("inputs", {}).items():
            # 引用完整性
            if isinstance(val, list) and len(val) == 2 and isinstance(val[0], str) and isinstance(val[1], int):
                ref_id = val[0]
                if ref_id not in graph:
                    raise ValueError(f"node {node_id}.{key} references missing node: {ref_id}")
            # 路径安全: 拒绝绝对路径和 ..
            # WHY: 只拦截真正的路径逃逸形态（以 / 或 .. 开头、或 /../ 中间穿越）。
            # 不能用 ".." in val 粗判：markdown 说明节点的省略号/列表写法
            # 也含连续两点，会被误判为不安全路径导致合法 workflow 被拒。
            if isinstance(val, str):
                if val.startswith("/") or val.startswith("..") or "/../" in val:
                    raise ValueError(f"node {node_id}.{key} contains unsafe path: {val[:80]}")
    if not has_output:
        raise ValueError("workflow graph has no SaveImage/PreviewImage output node")


# ---------------- output lifecycle management ----------------

_last_cleanup = 0.0


def cleanup_outputs() -> dict:
    """输出清理：PNG 满阈值才触发，删除最旧的到阈值以下。

    不做时间过期清理（uploads 是跑完即清，见 Worker.finally）。
    """
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < SETTINGS.cleanup_interval_s:
        return {}
    _last_cleanup = now
    removed = {"outputs": []}
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        png_files = sorted(
            [p for p in OUTPUT_DIR.iterdir() if p.is_file() and p.suffix == ".png"],
            key=lambda p: p.stat().st_mtime,
        )
        if len(png_files) >= SETTINGS.output_max_files:
            # 删最旧的，留 output_max_files - 10 张余量，避免每次提交都触发
            keep = max(0, SETTINGS.output_max_files - 10)
            for p in png_files[: len(png_files) - keep]:
                p.unlink(missing_ok=True)
                removed["outputs"].append(p.name)
                meta = p.with_suffix(".json")
                if meta.is_file():
                    meta.unlink(missing_ok=True)
    except Exception as e:
        removed["error"] = str(e)
    return removed


def _read_json_body(handler: BaseHTTPRequestHandler, max_bytes: int) -> dict:
    length = int(handler.headers.get("content-length", "0") or "0")
    if length <= 0:
        raise ValueError("empty request body")
    if length > max_bytes:
        raise ValueError(f"request body too large: {length} > {max_bytes}")
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise ValueError(f"invalid JSON: {e}")


class ComfyWorkflowList(RoutePlugin):
    method="GET"; paths=("/comfy/workflow/workflows","/v1/comfy/workflow/workflows")
    def handle(self, handler, config, plugin):
        items = []
        for meta in list_workflows():
            items.append({
                "file": meta.get("file"),
                "description": meta.get("description"),
                **({"error": meta["error"]} if meta.get("error") else {}),
            })
        send_json(handler, 200, {"workflows": items})
        return True


class ComfyWorkflowSurface(RoutePlugin):
    """返回 UI workflow 的顶层可编辑面（agent 只看/只改这一层）。

    subgraph 类模板: params = 顶层实例节点的 widgets_values_named（prompt/seed/
    steps/模型文件名等），connections = 顶层外部连线（如 ResolutionSelector →
    尺寸槽、上传图 → 参考图槽），image_inputs = 参考图槽位当前值。
    普通 UI 模板: params = 各顶层节点的 widget 值。
    内部 subgraph 结构不在返回里，agent 碰不到。
    """
    method="GET"; paths=(); prefixes=("/comfy/workflow/workflows/", "/v1/comfy/workflow/workflows/")
    suffix="/surface"
    def handle(self, handler, config, plugin):
        path = urlparse(handler.path).path
        wf_id = path.rsplit("/", 2)[-2] if path.endswith("/surface") else path.rsplit("/", 1)[-1]
        # workflows/<id>/surface 形式；按文件名直接解析（同 graph 端点）
        d = _wf_dir()
        wf_file = None
        if d and d.is_dir() and wf_id:
            for candidate in (wf_id, f"{wf_id}.json"):
                p = d / candidate
                if p.is_file():
                    wf_file = p
                    break
        if not wf_file:
            send_json(handler, 404, {"error": f"workflow file not found: {wf_id}"}); return True
        try:
            data = json.loads(wf_file.read_text(encoding="utf-8"))
            surface = extract_editable_surface(data)
        except ValueError as e:
            send_json(handler, 400, {"error": str(e)}); return True
        except Exception as e:
            send_json(handler, 500, {"error": f"editable surface failed: {e}"}); return True
        send_json(handler, 200, {"workflow": wf_id, **surface})
        return True


class ComfyGraphGet(RoutePlugin):
    """直接返回保存的 workflow 文件原文，agent 在本地改好后回传。"""
    method="GET"; paths=(); prefixes=("/comfy/workflow/graph/","/v1/comfy/workflow/graph/")
    def handle(self, handler, config, plugin):
        wf_id = urlparse(handler.path).path.rsplit("/", 1)[-1]
        d = _wf_dir()
        wf_file = None
        if d and d.is_dir() and wf_id:
            # WHY: 直接按文件名解析（完整名或 stem），不经过任何元数据/记忆层。
            for candidate in (wf_id, f"{wf_id}.json"):
                p = d / candidate
                if p.is_file():
                    wf_file = p
                    break
        if not wf_file:
            send_json(handler, 404, {"error": f"workflow file not found: {wf_id}"}); return True
        try:
            data = wf_file.read_bytes()  # 文件原文原样返回，不做任何包装
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json; charset=utf-8")
            handler.send_header("Content-Length", str(len(data)))
            handler.send_header("Access-Control-Allow-Origin", "*")
            handler.end_headers()
            handler.wfile.write(data)
        except Exception as e:
            send_json(handler, 500, {"error": f"workflow read failed: {e}"}); return True
        return True


class ComfyGraphSubmit(RoutePlugin):
    """agent 返回修改好的 workflow graph；插件校验后提交 ComfyUI。

    完整报错路径: 400（校验失败，返回具体 node/key/原因）→ 202（排队）→
    /jobs 轮询返回 ComfyUI 执行错误（含节点级信息）。
    """
    method="POST"; paths=("/comfy/workflow/graph/submit","/v1/comfy/workflow/graph/submit")
    def handle(self, handler, config, plugin):
        if not SETTINGS.ready():
            send_json(handler, 503, {
                "error": "comfy workflow plugin 未正确配置",
                "missing_options": SETTINGS.unconfigured(),
                "hint": "在 config.json 的 plugins[].options 里设置 comfy_url 与 workflows_dir",
            })
            return True
        # graph + 可选 files（multipart: workflow=<id>, graph=<json string>, image=<file>...）
        try:
            fields, files = read_multipart(handler)
        except Exception as e:
            send_json(handler, 400, {"error": f"multipart parse failed: {e}"}); return True

        graph_raw = fields.get("graph", "")
        if not graph_raw:
            send_json(handler, 400, {"error": "graph field is empty"}); return True
        if len(graph_raw.encode("utf-8")) > SETTINGS.workflow_max_bytes:
            send_json(handler, 400, {"error": f"graph too large: > {SETTINGS.workflow_max_bytes // 1024} KB"}); return True
        try:
            graph = json.loads(graph_raw)
        except Exception as e:
            send_json(handler, 400, {"error": f"invalid graph JSON: {e}"}); return True
        if _is_ui_workflow(graph):
            # WHY: agent 只提交顶层编辑后的 UI workflow；这里确定性地转成
            # API 格式，已知模板的 subgraph 内部结构用 canonical 定义锁定。
            try:
                graph = ui_workflow_to_api(graph, canonical_subgraphs())
            except ValueError as e:
                send_json(handler, 400, {"error": "workflow conversion failed", "detail": str(e)}); return True

        # 合规校验
        try:
            validate_workflow_graph(graph, SETTINGS)
        except ValueError as e:
            send_json(handler, 400, {"error": "workflow validation failed", "detail": str(e)}); return True

        # 保存上传文件；原始名 <-> 落盘名一一对应，返回给 agent 自己组织 graph
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        saved = []; upload_map = []; total = 0
        try:
            for field_name, filename, data in files:
                total += len(data)
                if total > UPLOAD_MAX:
                    raise ValueError("uploaded images exceed limit")
                suffix = Path(filename or "image.png").suffix.lower() or ".png"
                if suffix not in (".png", ".jpg", ".jpeg", ".webp"):
                    raise ValueError("supported formats: png/jpg/jpeg/webp")
                disk_name = f"{hashlib.sha256(data).hexdigest()}{suffix}"
                path = UPLOAD_DIR / disk_name
                path.write_bytes(data); saved.append(path)
                upload_map.append({
                    "field": field_name,
                    "original_name": filename or "",
                    "disk_name": disk_name,  # 复制到 ComfyUI input/ 后仍用此名
                })
        except Exception as e:
            for pp in saved: pp.unlink(missing_ok=True)
            send_json(handler, 400, {"error": str(e)}); return True

        job_id = uuid.uuid4().hex
        job = Job(job_id, {"graph": graph_raw}, saved, graph=graph)
        with worker.lock:
            worker.history[job_id] = job
        worker.q.put(job)
        # 输出清理：满 output_max_files 张才触发
        removed = cleanup_outputs()
        send_json(handler, 202, {**job.public(), "uploaded_files": upload_map, "cleanup": removed or None})
        return True


class ComfyHealth(RoutePlugin):
    method="GET"; paths=("/comfy/workflow/health","/v1/comfy/workflow/health"); auth_exempt=True
    def handle(self, handler, config, plugin):
        missing = SETTINGS.unconfigured()
        send_json(handler, 200, {
            # WHY: 配置不完整时必须显式报不健康 + 缺什么, 绝不能返回 ok=true
            # 然后在真正生成时才崩。
            "ok": SETTINGS.ready(),
            "configured": not missing,
            "missing_options": missing,
            "comfy_url": SETTINGS.comfy_url,
            "workflows_dir": str(SETTINGS.workflows_dir) if SETTINGS.workflows_dir else None,
            "model": SETTINGS.model_name, "plugin": "comfy-workflow",
            "queue_length": worker.q.qsize(), "running": worker.current is not None,
        })
        return True


class ComfyStatus(RoutePlugin):
    method="GET"; paths=("/comfy/workflow/jobs","/v1/comfy/workflow/jobs")
    def handle(self, handler, config, plugin):
        qs=parse_qs(urlparse(handler.path).query)
        job_id=(qs.get("job_id") or [""])[0]
        if job_id:
            job=find_job(job_id)
            if job:
                send_json(handler,200,job.public()); return True
            info=completed_info(job_id)
            if info:
                send_json(handler,200,info); return True
            log=LOG_DIR/f"{job_id}.log"
            if log.is_file():
                send_json(handler,200,{"job_id":job_id,"status":"failed","log_path":str(log)}); return True
            send_json(handler,404,{"error":"job not found"}); return True
        send_json(handler,200,{"running":worker.current.public() if worker.current else None,
                               "queued":[j.public() for j in list(worker.q.queue)],
                               "history":[j.public() for j in worker.history.values()]})
        return True


class ComfyResult(RoutePlugin):
    method="GET"; paths=("/comfy/workflow/result","/v1/comfy/workflow/result")
    def handle(self, handler, config, plugin):
        qs=parse_qs(urlparse(handler.path).query)
        job_id=(qs.get("job_id") or [""])[0]
        try:
            p=safe_output(job_id)
        except FileNotFoundError:
            send_json(handler,404,{"error":"output not found"}); return True
        except Exception:
            send_json(handler,400,{"error":"invalid job id"}); return True
        data=p.read_bytes()
        handler.send_response(200)
        handler.send_header("Content-Type","image/png")
        handler.send_header("Content-Length",str(len(data)))
        handler.send_header("Access-Control-Allow-Origin","*")
        handler.end_headers()
        handler.wfile.write(data)
        return True


class ComfyDelete(RoutePlugin):
    method="DELETE"; paths=(); prefixes=("/comfy/workflow/jobs/","/v1/comfy/workflow/jobs/")
    def handle(self, handler, config, plugin):
        job_id=urlparse(handler.path).path.rsplit("/",1)[-1]
        if find_job(job_id):
            send_json(handler,409,{"error":"job is queued or running"}); return True
        deleted=[]
        for p in (OUTPUT_DIR/f"{job_id}.png",OUTPUT_DIR/f"{job_id}.json"):
            if p.is_file():
                p.unlink(); deleted.append(p.name)
        for p in UPLOAD_DIR.glob(f"{job_id}*"):
            if p.is_file(): p.unlink(); deleted.append(p.name)
        if not deleted:
            send_json(handler,404,{"error":"job not found"}); return True
        send_json(handler,200,{"deleted":deleted}); return True


def routes():
    worker.start_once()
    return [ComfyGraphGet(), ComfyGraphSubmit(),
            ComfyWorkflowList(), ComfyWorkflowSurface(),
            ComfyHealth(), ComfyStatus(), ComfyResult(), ComfyDelete()]
