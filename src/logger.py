"""SHTUCodeProxy — 日志模块

职责: 日志级别控制、日志写入、orjson 封装、JSON 工具函数

日志级别:
   0 = 静默（不输出任何日志）
   1 = 仅错误
   2 = 信息（默认）
   3 = 详细

注: config.json 未设置 log_level 时 (默认 -1) 视为未配置, 自动 fallback 到
     SHTU_LOG_LEVEL 环境变量或默认 2. 想完全关闭日志请显式设 log_level=0 或
     SHTU_LOG_LEVEL=0 (设 SHTU_LOG_LEVEL=-1 也完全静默, 与 0 等价).

优先级: config.json log_level > 环境变量 SHTU_LOG_LEVEL > 默认值 2
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from platform_utils import app_dir
from config_store import AppConfig

# 性能优化: 优先使用 orjson (2-10x faster than stdlib json)
try:
    import orjson as _orjson
    _HAS_ORJSON = True
except ImportError:  # pragma: no cover
    import json as _orjson  # type: ignore
    _HAS_ORJSON = False


# ---------------------------------------------------------------------------
# orjson 封装
# ---------------------------------------------------------------------------

def _orjson_dumps(obj: Any) -> bytes:
    """统一的 JSON 序列化入口 (返回 bytes)."""
    if _HAS_ORJSON:
        return _orjson.dumps(obj)
    return _orjson.dumps(obj).encode("utf-8")


def _orjson_dumps_str(obj: Any) -> str:
    """返回字符串形式的 JSON (用于需要 str 而非 bytes 的场景)."""
    if _HAS_ORJSON:
        return _orjson.dumps(obj).decode("utf-8")
    return _orjson.dumps(obj)


def _orjson_loads(data: Any) -> Any:
    """统一的 JSON 解析入口."""
    if _HAS_ORJSON:
        if isinstance(data, (bytes, bytearray)):
            return _orjson.loads(data)
        if isinstance(data, str):
            return _orjson.loads(data.encode("utf-8"))
        return _orjson.loads(data)
    return _orjson.loads(data)


def json_dumps_compact(value: Any) -> str:
    """紧凑 JSON 输出 (orjson 默认即紧凑, stdlib 需 separators)."""
    if _HAS_ORJSON:
        return _orjson_dumps_str(value)
    import json
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 日志级别
# ---------------------------------------------------------------------------

LOG_FILE_MAX_BYTES = 1 * 1024 * 1024  # ponytail: 1MB — 触发更频繁但保留 3 个备份 = 4MB 历史窗口
LOG_FILE_BACKUP_COUNT = 3  # ponytail: 3 个 .1/.2/.3 备份，足够覆盖一次事故复盘
LOG_FILE_NAME = "proxy.log"

# 模块级缓存: current_config() 尚未就绪时使用
_LOG_LEVEL = int(os.getenv("SHTU_LOG_LEVEL", "2"))

# 由 proxy 模块在启动时注册，避免循环导入
_ACTIVE_CONFIG_REF = None


def register_active_config(config_getter) -> None:
    """由 proxy 模块调用，注册获取当前配置的回调。"""
    global _ACTIVE_CONFIG_REF
    _ACTIVE_CONFIG_REF = config_getter


def current_config() -> AppConfig:
    """获取当前活跃配置。通过 register_active_config 注册的回调获取。"""
    if _ACTIVE_CONFIG_REF is not None:
        return _ACTIVE_CONFIG_REF()
    return AppConfig.default()


def _get_log_level() -> int:
    """动态获取日志级别, 优先 config.json, 其次环境变量, 默认 2.

    config.json log_level=-1 视为 "未配置", fallback 到环境变量 (与 AppConfig.default 注释一致).
    ponytail: 顺手修 pre-existing bug — 原代码把 -1 当合法值直接返回,
    导致未配置部署永远只写 stderr 不写文件, 跟 docstring 承诺的 fallback 行为不符.
    """
    try:
        cfg = current_config()
        cl = getattr(cfg, "log_level", -1)
        if isinstance(cl, int) and 0 <= cl <= 3:
            return cl
    except Exception:
        pass
    return int(os.getenv("SHTU_LOG_LEVEL", "2"))


# ---------------------------------------------------------------------------
# 时间工具
# ---------------------------------------------------------------------------

def now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# 日志写入
# ---------------------------------------------------------------------------

# ponytail: 用 stdlib logging + RotatingFileHandler 替换手写轮转
# 自带锁（线程安全）、原子 rename（process crash 不丢日志）、延迟写（不用每条 open/close）。
# 阈值改 1MB×3 备份（原 5MB×1 太激进 — 一旦轮转就把所有老日志 unlink 掉）。
# 公开 API (log_info/log_error/log_debug/log) 签名不变，proxy.py / transformer.py 零改动。

_logger = logging.getLogger("shtu_proxy")
_logger.setLevel(logging.DEBUG)  # 由 handler 决定是否过滤
_logger.propagate = False  # 不要冒泡到 root logger
_handlers_lock = threading.Lock()
_handlers_installed = False


def _build_handlers() -> list[logging.Handler]:
    handlers: list[logging.Handler] = []

    class _DynamicStderrHandler(logging.StreamHandler):
        """每次 emit 时取当前 sys.stderr, 让测试/调用方可重定向 stderr. ponytail: 避免 handler 锁死初始 stderr."""

        def emit(self, record):
            self.stream = sys.stderr
            super().emit(record)

    # stderr — 永远保留（除非用户在 SHTU_STDERR_LOG=0 显式关）
    if os.getenv("SHTU_STDERR_LOG", "1") != "0":
        stderr_h = _DynamicStderrHandler()
        stderr_h.setFormatter(logging.Formatter("%(message)s"))
        handlers.append(stderr_h)
    # file — log_level=-1 时跳过
    if _get_log_level() != -1:
        try:
            target_dir = app_dir()
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / LOG_FILE_NAME
            file_h = logging.handlers.RotatingFileHandler(
                target,
                maxBytes=LOG_FILE_MAX_BYTES,
                backupCount=LOG_FILE_BACKUP_COUNT,
                encoding="utf-8",
                delay=True,  # ponytail: 启动时不创建空文件
            )
            file_h.setFormatter(logging.Formatter("%(message)s"))
            handlers.append(file_h)
        except Exception:
            pass  # disk full / permission denied — stderr 还在
    return handlers


def _ensure_handlers() -> None:
    global _handlers_installed
    if _handlers_installed:
        return
    with _handlers_lock:
        if _handlers_installed:
            return
        for h in _build_handlers():
            _logger.addHandler(h)
        _handlers_installed = True


def _write_log(line: str) -> None:
    """底层日志写入。handlers 各自按初始化时配置决定是否接收, 本函数不做级别过滤."""
    _ensure_handlers()
    _logger.debug(line)  # 全部走 DEBUG，handler 端无级别过滤，由调用方 log_*/log_error 控制


def log(message: str) -> None:
    """向后兼容的无条件日志输出。新代码请用 log_error/log_info/log_debug。"""
    if _get_log_level() <= 0:
        return  # 0=静默, -1=不启用
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    _write_log(line)


def log_error(message: str) -> None:
    """仅 log_level>=1 时输出 (错误级别)。log_level=-1 表示完全关闭 (包括 stderr)."""
    if _get_log_level() < 1:
        return
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    _write_log(line)


def log_info(message: str) -> None:
    """仅 log_level>=2 时输出 (信息级别，默认)。"""
    if _get_log_level() < 2:
        return
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    _write_log(line)


def log_debug(message: str) -> None:
    """仅 log_level>=3 时输出 (详细日志)。"""
    if _get_log_level() < 3:
        return
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    _write_log(line)


def usage_cache_debug(usage: Any) -> str:
    if not isinstance(usage, dict):
        return ""
    candidates = {
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "cached_tokens": usage.get("cached_tokens"),
    }
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details")
    if isinstance(input_details, dict):
        candidates["details_cached_tokens"] = input_details.get("cached_tokens")
    present = {key: value for key, value in candidates.items() if value is not None}
    if not present:
        return ""
    # 性能优化: 用 orjson 替代 json
    return " cache_usage=" + _orjson_dumps_str(present)


def usage_summary(usage: Any) -> str:
    if not isinstance(usage, dict):
        return ""
    parts = []
    inp = usage.get("input_tokens")
    if isinstance(inp, (int, float)):
        parts.append(f"in={int(inp)}")
    out = usage.get("output_tokens")
    if isinstance(out, (int, float)):
        parts.append(f"out={int(out)}")
    return (" " + " ".join(parts)) if parts else ""
