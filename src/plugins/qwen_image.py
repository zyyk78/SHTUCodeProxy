"""Qwen Image 2.1 route plugin for SHTUCodeProxy.

本文件**不包含任何部署相关的本地路径**。模型目录、数据目录、脚本位置和各项
上限全部来自 config.json 的插件 options 段（也可由环境变量覆盖）:

    "plugins": [
      {
        "module": "qwen_image.py",
        "enabled": true,
        "timeout": 600,
        "options": {
          "model_root": "/path/to/qwen-image-2.1",
          "data_root": "/path/to/qwen-image-data",
          "run_script": "scripts/run.sh",
          "prompt_max": 8000,
          "upload_max_mb": 50,
          "offline": true
        }
      }
    ]

``model_root`` 是唯一必填项：缺失时插件不会猜测任何路径，而是让
``/qwen/image/health`` 返回 ok=false 并说明原因，生成接口返回 503。
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from plugin_manager import RoutePlugin


class Settings:
    """插件的部署相关配置。全部可由 config.json / 环境变量提供。"""

    def __init__(self, options: Optional[dict] = None):
        opts = options if isinstance(options, dict) else {}
        model_root = _first_set(opts.get("model_root"), os.environ.get("QWEN_IMAGE_ROOT"))
        self.model_root: Optional[Path] = Path(model_root).expanduser().resolve() if model_root else None
        data_root = _first_set(opts.get("data_root"), os.environ.get("QWEN_IMAGE_API_DATA_ROOT"))
        # data_root 未配置时落到用户态目录（不写死任何用户名）。
        self.data_root: Path = Path(
            data_root if data_root else Path.home() / ".local" / "share" / "qwen-image-api"
        ).expanduser().resolve()
        run_script = _first_set(opts.get("run_script"), os.environ.get("QWEN_IMAGE_RUN_SCRIPT"))
        self.run_script: Optional[Path] = (
            (self.model_root / run_script).expanduser()
            if self.model_root and run_script else
            (Path(run_script).expanduser().resolve() if run_script else None)
        )
        self.prompt_max = _to_int(opts.get("prompt_max"), os.environ.get("QWEN_IMAGE_PROMPT_MAX"), 8000)
        self.upload_max_bytes = _to_int(opts.get("upload_max_mb"), os.environ.get("QWEN_IMAGE_UPLOAD_MAX_MB"), 50) * 1024 * 1024
        self.offline = _to_bool(opts.get("offline"), os.environ.get("QWEN_IMAGE_OFFLINE"), True)
        self.model_name = str(opts.get("model_name") or "Qwen-Image-2.1")

    @property
    def output_dir(self) -> Path:
        return self.data_root / "outputs"

    @property
    def upload_dir(self) -> Path:
        return self.data_root / "uploads"

    @property
    def log_dir(self) -> Path:
        return self.data_root / "logs"

    def unconfigured(self) -> list:
        """列出缺失的必填项（空列表 = 配置完整）。"""
        missing = []
        if not self.model_root:
            missing.append("model_root")
        if not self.run_script:
            missing.append("run_script")
        return missing

    def ready(self) -> bool:
        return not self.unconfigured() and bool(self.run_script and self.run_script.is_file())


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


def configure(options: Optional[dict] = None) -> None:
    """由 plugin_manager 在加载时调用一次（routes() 之前）。"""
    global SETTINGS, DATA_ROOT, OUTPUT_DIR, UPLOAD_DIR, LOG_DIR
    global PROMPT_MAX, UPLOAD_MAX
    SETTINGS = Settings(options)
    DATA_ROOT = SETTINGS.data_root
    OUTPUT_DIR = SETTINGS.output_dir
    UPLOAD_DIR = SETTINGS.upload_dir
    LOG_DIR = SETTINGS.log_dir
    PROMPT_MAX = SETTINGS.prompt_max
    UPLOAD_MAX = SETTINGS.upload_max_bytes


def _run_script() -> Optional[Path]:
    return SETTINGS.run_script


def _env() -> dict:
    env = dict(os.environ)
    if SETTINGS.offline:
        env["HF_HUB_OFFLINE"] = "1"
    return env


class Job:
    def __init__(self, job_id: str, form: dict, files: list[Path]):
        self.id = job_id
        self.form = form
        self.files = files
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
            self.thread = threading.Thread(target=self.run, daemon=True, name="qwen-image-worker")
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
                run_script = _run_script()
                if not run_script or not run_script.is_file():
                    raise RuntimeError(
                        "qwen image 未正确配置: 请在 config.json 的 plugins[].options 里设置 "
                        "model_root 与 run_script (缺失项: %s)" % (SETTINGS.unconfigured() or ["run_script 不存在"])
                    )
                f = job.form
                cmd = [str(run_script), "-p", f["prompt"]]
                cmd += ["--width", str(f["width"]), "--height", str(f["height"])]
                cmd += ["--steps", str(f["steps"]), "--seed", str(f["seed"]), "--cfg", str(f["cfg"])]
                if f.get("device_map"):
                    cmd += ["--device-map", f["device_map"]]
                elif f.get("gpu") is not None:
                    cmd += ["--gpu", str(f["gpu"])]
                if f.get("group_offload"): cmd += ["--group-offload"]
                if f.get("attn_slicing"): cmd += ["--attn-slicing"]
                if f.get("rgba"): cmd += ["--rgba"]
                if job.files: cmd += ["--input", ",".join(map(str, job.files))]
                cmd += ["--out", str(OUTPUT_DIR / f"{job.id}.png")]
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                job.log = LOG_DIR / f"{job.id}.log"
                with job.log.open("w") as log:
                    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                            cwd=str(SETTINGS.model_root) if SETTINGS.model_root else None,
                                            env=_env())
                    job.returncode = proc.wait()
                if job.returncode == 0:
                    job.output = OUTPUT_DIR / f"{job.id}.png"
                    if not job.output.is_file(): raise RuntimeError("output missing")
                    job.status = "completed"
                else:
                    job.status = "failed"
                    job.error = f"generate.py exited {job.returncode}; see log_path"
            except Exception as e:
                job.status = "failed"; job.error = str(e)
                if job.returncode is None: job.returncode = -1
            finally:
                job.elapsed = round(time.monotonic() - t0, 1)
                job.finished = datetime.now().isoformat(timespec="seconds")
                with self.lock: self.current = None


worker = Worker(start=bool(os.environ.get("QWEN_IMAGE_PLUGIN_AUTOSTART", "1") not in ("0", "false", "no", "off")))


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


class QwenHealth(RoutePlugin):
    method="GET"; paths=("/qwen/image/health","/v1/qwen/image/health"); auth_exempt=True
    def handle(self, handler, config, plugin):
        missing = SETTINGS.unconfigured()
        run_script = _run_script()
        script_exists = bool(run_script and run_script.is_file())
        send_json(handler, 200, {
            # WHY: 配置不完整时必须显式报不健康 + 缺什么, 绝不能返回 ok=true
            # 然后在真正生成时才崩, 也不能悄悄指向某个猜测的本地路径。
            "ok": SETTINGS.ready(),
            "configured": not missing,
            "missing_options": missing,
            "run_script_exists": script_exists,
            "model": SETTINGS.model_name, "plugin": "qwen-image",
            "queue_length": worker.q.qsize(), "running": worker.current is not None,
        })
        return True


class QwenGenerate(RoutePlugin):
    method="POST"; paths=("/qwen/image/generations","/v1/qwen/image/generations")
    def handle(self, handler, config, plugin):
        # WHY: 配置不完整时直接 503, 不要收了请求再排队失败。
        if not SETTINGS.ready():
            send_json(handler, 503, {
                "error": "qwen image plugin 未正确配置",
                "missing_options": SETTINGS.unconfigured() or (["run_script 不存在"] if _run_script() else []),
                "hint": "在 config.json 的 plugins[].options 里设置 model_root 与 run_script",
            })
            return True
        try:
            fields, files = read_multipart(handler)
        except Exception as e:
            send_json(handler, 400, {"error": str(e)}); return True
        prompt=fields.get("prompt","").strip()
        if not prompt:
            send_json(handler,400,{"error":"prompt is empty"}); return True
        if len(prompt)>PROMPT_MAX:
            send_json(handler,400,{"error":"prompt too long"}); return True
        try:
            width=int(fields.get("width",1024)); height=int(fields.get("height",1024))
            steps=int(fields.get("steps",40)); seed=int(fields.get("seed",42)); cfg=float(fields.get("cfg",1.0))
            gpu=int(fields["gpu"]) if fields.get("gpu") else None
        except Exception:
            send_json(handler,400,{"error":"invalid numeric parameter"}); return True
        if width<256 or height<256 or width>2752 or height>2752 or width%16 or height%16:
            send_json(handler,400,{"error":"width/height must be 256..2752 and multiples of 16"}); return True
        if not 1<=steps<=100:
            send_json(handler,400,{"error":"steps must be 1..100"}); return True
        device_map=fields.get("device_map") or None
        if device_map not in (None,"balanced"):
            send_json(handler,400,{"error":"device_map only supports balanced"}); return True
        group_offload=fields.get("group_offload","").lower() in ("1","true","yes","on")
        attn_slicing=fields.get("attn_slicing","").lower() in ("1","true","yes","on")
        rgba=fields.get("rgba","").lower() in ("1","true","yes","on")
        if device_map=="balanced" and (group_offload or gpu is not None):
            send_json(handler,422,{"error":"balanced cannot be combined with group_offload/gpu"}); return True

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        saved=[]; total=0
        try:
            for field_name, filename, data in files:
                total += len(data)
                if total > UPLOAD_MAX:
                    raise ValueError("uploaded images exceed 50 MB")
                suffix=Path(filename or "image.png").suffix.lower() or ".png"
                if suffix not in (".png",".jpg",".jpeg",".webp"):
                    raise ValueError("supported formats: png/jpg/jpeg/webp")
                path=UPLOAD_DIR/f"{uuid.uuid4().hex}{suffix}"
                path.write_bytes(data); saved.append(path)
        except Exception as e:
            for p in saved: p.unlink(missing_ok=True)
            send_json(handler,400,{"error":str(e)}); return True

        job_id=uuid.uuid4().hex
        form={"prompt":prompt,"width":width,"height":height,"steps":steps,"seed":seed,
              "cfg":cfg,"gpu":gpu,"device_map":device_map,"group_offload":group_offload,
              "attn_slicing":attn_slicing,"rgba":rgba}
        job=Job(job_id, form, saved)
        with worker.lock:
            worker.history[job_id] = job
        worker.q.put(job)
        send_json(handler,202,job.public())
        return True


class QwenStatus(RoutePlugin):
    method="GET"; paths=("/qwen/image/jobs","/v1/qwen/image/jobs")
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


class QwenResult(RoutePlugin):
    method="GET"; paths=("/qwen/image/result","/v1/qwen/image/result")
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


class QwenDelete(RoutePlugin):
    method="DELETE"; paths=(); prefixes=("/qwen/image/jobs/","/v1/qwen/image/jobs/")
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
    return [QwenHealth(), QwenGenerate(), QwenStatus(), QwenResult(), QwenDelete()]
