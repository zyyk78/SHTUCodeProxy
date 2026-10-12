# idle_vram_guard.py — ComfyUI 空闲显存看门狗
#
# 功能: 队列空 + 空闲超过阈值 → 直接卸载全部模型并清缓存。
# 解决的问题: ComfyUI 自身的 sticky /free flag 只在「下个任务结束后」才生效,
#              如果一直没新任务, 模型就永远驻留在显存里白占资源。
#
# 触发逻辑: 每个空闲周期只卸载一次。空闲到点 → 卸载并置 armed=True,
#           直到有新任务到来才重置 armed=False。空卡重复卸载是无害的
#           空操作, 但没必要每 30 秒打一遍日志, 所以用 armed 防重复。
#
# 与 SHTUProxy 插件侧看门狗的关系:
#   两者语义相同, 但本节点在 ComfyUI 进程内直接调用内部 API, 立即生效,
#   且天然感知网页版提交的任务 (读的是同一个 PromptQueue)。
#   推荐启用本节点后把插件侧 vram_unload_idle_s 设为 0 (关闭), 避免双看门狗。
#
# 配置 (环境变量):
#   COMFY_IDLE_VRAM_GUARD_IDLE_S      空闲秒数阈值, 默认 1800 (30 分钟)
#   COMFY_IDLE_VRAM_GUARD_INTERVAL_S  检查间隔秒数, 默认 30
#   设 COMFY_IDLE_VRAM_GUARD_IDLE_S=0 可禁用本节点 (不启动看门狗线程)。
#
# 安装: 放入 ComfyUI/custom_nodes/ 后重启 ComfyUI。
# 卸载: 删除本文件, 重启 ComfyUI。

import logging
import os
import threading
import time

import torch


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


IDLE_S = _env_int("COMFY_IDLE_VRAM_GUARD_IDLE_S", 1800)
INTERVAL_S = _env_int("COMFY_IDLE_VRAM_GUARD_INTERVAL_S", 30)

# 运行时状态, 供 Status 节点和外部诊断读取
_state = {
    "last_check": None,       # 最近一次检查的 time.time()
    "last_busy": None,        # 最近一次队列非空的 time.time()
    "last_unload": None,      # 最近一次成功卸载的 time.time()
    "unload_count": 0,
    "armed": False,           # 当前空闲周期是否已卸载过
    "enabled": IDLE_S > 0,
}


def _get_queue_remaining() -> int:
    """读取 ComfyUI 全局 PromptQueue 的剩余任务数 (running + pending)。

    返回 -1 表示 PromptServer 尚未初始化 (不应发生在正常启动流程中)。
    """
    try:
        from server import PromptServer
        ps = PromptServer.instance
        if ps is None or not hasattr(ps, "prompt_queue"):
            return -1
        return ps.prompt_queue.get_tasks_remaining()
    except Exception:
        return -1


def _get_vram_used_gb() -> float:
    """返回任一可见 CUDA 设备的最大已分配显存 (GB), 仅用于日志诊断。"""
    max_gb = 0.0
    for i in range(torch.cuda.device_count()):
        try:
            gb = torch.cuda.memory_allocated(i) / (1024 ** 3)
            if gb > max_gb:
                max_gb = gb
        except Exception:
            pass
    return max_gb


def _do_unload():
    """调用 ComfyUI 内部 API 卸载全部模型并清缓存 (与工具栏 Free 按钮等价)。"""
    import comfy.model_management as mm
    mm.unload_all_models()
    mm.soft_empty_cache()


def _watchdog_loop():
    logging.info(
        f"[idle_vram_guard] started: idle_s={IDLE_S} interval_s={INTERVAL_S}"
    )
    while True:
        time.sleep(INTERVAL_S)
        now = time.time()
        _state["last_check"] = now

        remaining = _get_queue_remaining()
        if remaining < 0:
            # PromptServer 不可用 (罕见), 跳过本轮
            continue
        if remaining > 0:
            # 有任务在跑或排队 → 重置空闲计时, 撤销 armed
            _state["last_busy"] = now
            _state["armed"] = False
            continue

        # 队列空: 计算空闲时长
        last_busy = _state["last_busy"]
        if last_busy is None:
            # 启动后从未有任务, 以启动时刻为基准
            _state["last_busy"] = now
            continue
        idle = now - last_busy
        if idle < IDLE_S:
            continue

        # 空闲到点且本周期未卸载过 → 执行
        if _state["armed"]:
            continue
        try:
            vram_gb = _get_vram_used_gb()
            _do_unload()
            _state["last_unload"] = now
            _state["unload_count"] += 1
            _state["armed"] = True
            logging.info(
                f"[idle_vram_guard] unloaded models: idle={idle:.0f}s "
                f"vram_before={vram_gb:.1f}GB"
            )
        except Exception as e:
            logging.warning(f"[idle_vram_guard] unload failed: {e}")


def _start_watchdog():
    if not _state["enabled"]:
        logging.info("[idle_vram_guard] disabled (COMFY_IDLE_VRAM_GUARD_IDLE_S=0)")
        return
    t = threading.Thread(target=_watchdog_loop, daemon=True, name="idle-vram-guard")
    t.start()


class IdleVRAMGuardStatus:
    """一个极简的展示节点, 让用户能在 ComfyUI 画布上查看看门狗状态。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("status",)
    FUNCTION = "get_status"
    OUTPUT_NODE = True
    CATEGORY = "utils/system"

    def get_status(self):
        now = time.time()
        last_busy = _state.get("last_busy")
        idle = f"{now - last_busy:.0f}s" if last_busy else "N/A"
        lines = [
            f"enabled={_state['enabled']}",
            f"idle_threshold={IDLE_S}s",
            f"current_idle={idle}",
            f"armed={_state['armed']}",
            f"unload_count={_state['unload_count']}",
        ]
        return (" | ".join(lines),)


NODE_CLASS_MAPPINGS = {
    "IdleVRAMGuardStatus": IdleVRAMGuardStatus,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "IdleVRAMGuardStatus": "Idle VRAM Guard Status",
}

# 模块加载时启动看门狗线程
_start_watchdog()
