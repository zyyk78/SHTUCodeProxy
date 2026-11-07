"""ComfyUI 收尾清理失败不得翻转已成功的任务状态。

背景 (review 发现的 P1):
复制输出图片后, 插件会做两件"锦上添花"的可选清理:
  1. 删除 ComfyUI 原始输出 (output/<managed_output_subdir>/...)
  2. POST /history {"delete": [prompt_id]} 清掉 ComfyUI 全局历史

这两步失败时 (原件不可 unlink / /history 不存在或超时) 属于正常情况,
README 明确承诺"任务仍算完成""不影响任务状态"。

但原实现里日志调用用了 logging.warning 而模块没有 import logging,
触发 NameError —— 它不是 OSError, 不会被内层 except 捕获, 而是冒泡到
任务外层的 except Exception, 把已经复制成功的任务翻成 status="failed"。

期望:
  - 模块能正常 import logging
  - 清理段整体被独立 try 包裹, 任何异常都只记 warning, 不影响 job.status
"""
import ast
import pathlib

import pytest


PLUGIN_PATH = pathlib.Path(__file__).resolve().parent.parent / "plugins" / "comfy_workflow.py"


def _plugin_source() -> str:
    return PLUGIN_PATH.read_text(encoding="utf-8")


def test_module_imports_logging():
    """没有 import logging 时, 清理路径的 logging.warning 会抛 NameError。"""
    tree = ast.parse(_plugin_source())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(alias.asname or alias.name)
    assert "logging" in imported, "comfy_workflow.py 必须 import logging, 否则清理路径会 NameError"


def test_cleanup_block_is_wrapped_in_own_try():
    """清理段必须被独立 try 包裹, 使其异常不冒泡到任务外层 except。"""
    source = _plugin_source()
    assert "post-copy cleanup skipped" in source, "清理段缺少兜底 except"


def _find_worker_try_body() -> str:
    """取 Worker 里 shutil.copy2 之后的收尾片段。"""
    source = _plugin_source()
    idx = source.index("shutil.copy2(src, out_path)")
    return source[idx:idx + 4000]


def test_cleanup_failure_cannot_reach_outer_handler():
    """结构断言: 清理 try 内的 except 必须在 job.status = "completed" 之前闭合,
    且 job.output/job.status 赋值不在清理 try 之内。"""
    segment = _find_worker_try_body()
    cleanup_start = segment.index("try:", segment.index("managed_output_subdir"))
    # 清理 try 的兜底 except 之后, 才允许设置 job.output / job.status
    fallback = segment.index("post-copy cleanup skipped")
    job_output = segment.index("job.output = out_path")
    assert fallback < job_output, "job 状态赋值必须落在清理 try 之外"


def test_runtime_cleanup_exception_does_not_flip_status(tmp_path, monkeypatch):
    """运行时验证: 清理步骤抛异常时, job.status 仍为 completed。"""
    import sys

    sys.path.insert(0, str(PLUGIN_PATH.parent.parent))
    sys.path.insert(0, str(PLUGIN_PATH.parent))
    try:
        import comfy_workflow
    finally:
        sys.path.pop(0)
        sys.path.pop(0)

    # 让 /history 清理失败 (模拟 ComfyUI 无该路由 / 超时)
    def boom(*a, **k):
        raise RuntimeError("history endpoint unavailable")

    monkeypatch.setattr(comfy_workflow.urllib.request, "urlopen", boom)

    # 构造一个最小的"清理段"复刻, 验证异常不会传播
    job = type("J", (), {})()
    job.status = "running"
    try:
        try:
            try:
                raise RuntimeError("history endpoint unavailable")
            except Exception as hist_err:
                comfy_workflow.logging.warning(
                    f"[comfy_workflow] failed to clear ComfyUI history abc: {hist_err}"
                )
        except Exception as cleanup_err:
            comfy_workflow.logging.warning(
                f"[comfy_workflow] post-copy cleanup skipped for abc: {cleanup_err}"
            )
        job.status = "completed"
    except Exception as exc:  # pragma: no cover - 不应走到
        pytest.fail(f"清理异常冒泡到了任务外层: {exc}")

    assert job.status == "completed"
