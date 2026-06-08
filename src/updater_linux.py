# -*- coding: utf-8 -*-
"""Linux-specific update replacement logic for SHTUCodeProxy.

On Linux, the typical deployment is via the python-launcher tar.xz directory
or the headless CLI zip. The auto-update strategy replaces the launcher script
and the Python source files in-place.

For the single-file shtucodeproxyctl binary, the strategy mirrors Windows:
rename old → write new → restart.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

ROLLBACK_MARKER = "SHTUCodeProxy-update-rollback"
CLEANUP_FLAG = "--update-cleanup"
ROLLBACK_GRACE_SECONDS = 5


def _running_exe_path() -> Optional[Path]:
    """Return the path of the currently running executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    # Source mode — the launcher script
    main_script = Path(sys.argv[0]).resolve()
    if main_script.exists():
        return main_script
    return None


def _rollback_marker_path() -> Optional[Path]:
    exe = _running_exe_path()
    if not exe:
        return None
    return exe.parent / f".{ROLLBACK_MARKER}"


def _old_path() -> Optional[Path]:
    exe = _running_exe_path()
    if not exe:
        return None
    return Path(str(exe) + ".old")


def check_rollback_needed() -> bool:
    marker = _rollback_marker_path()
    return marker is not None and marker.exists()


def perform_rollback() -> Tuple[bool, Optional[str]]:
    exe = _running_exe_path()
    old = _old_path()
    marker = _rollback_marker_path()

    if not exe or not old:
        return False, "Cannot determine paths for rollback"

    if not old.exists():
        if marker:
            try:
                marker.unlink()
            except Exception:
                pass
        return False, "No .old backup found"

    try:
        try:
            exe.unlink()
        except PermissionError:
            return False, f"Cannot remove current file (locked): {exe}"

        import shutil
        shutil.move(str(old), str(exe))
        # Ensure executable bit is set
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)

        if marker:
            try:
                marker.unlink()
            except Exception:
                pass
        return True, None
    except Exception as e:
        return False, f"Rollback failed: {e}"


def apply_update(
    new_binary_path: Path,
    *,
    restart_proxy: bool = True,
) -> Tuple[bool, Optional[str]]:
    """Apply a downloaded update on Linux.

    For frozen binaries (shtucodeproxyctl): same rename-replace strategy as Windows.
    For source/launcher deployments: this is not supported — prompt user to re-download.
    """
    exe = _running_exe_path()
    old = _old_path()
    marker = _rollback_marker_path()

    if not exe:
        return False, "Cannot determine running executable path"

    if not getattr(sys, "frozen", False):
        return False, (
            "Auto-update is not supported for source/launcher deployments on Linux. "
            "Please download the latest release from GitHub."
        )

    if not new_binary_path.exists():
        return False, f"New binary not found: {new_binary_path}"

    try:
        # Write rollback marker
        if marker:
            marker.write_text(str(exe), encoding="utf-8")

        # Remove previous .old
        if old and old.exists():
            try:
                old.unlink()
            except Exception:
                old2 = Path(str(old) + f".{int(time.time())}")
                try:
                    old.rename(old2)
                except Exception:
                    pass

        # Rename current → .old
        try:
            exe.rename(old)
        except PermissionError as e:
            if marker:
                try:
                    marker.unlink()
                except Exception:
                    pass
            return False, f"Cannot rename running binary: {e}"

        # Copy new binary to original path
        import shutil
        try:
            shutil.copy2(str(new_binary_path), str(exe))
            exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        except Exception as e:
            # Restore
            try:
                old.rename(exe)
            except Exception:
                pass
            if marker:
                try:
                    marker.unlink()
                except Exception:
                    pass
            return False, f"Cannot copy new binary: {e}"

        # Start new binary
        cmd = [str(exe)]
        if restart_proxy:
            cmd.append("--start-proxy")
        cmd.append(CLEANUP_FLAG)

        try:
            subprocess.Popen(
                cmd,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            return False, f"Failed to start new binary: {e}"

        return True, None

    except Exception as e:
        return False, f"Update apply failed: {e}"


def cleanup_after_update() -> None:
    """Called by the new process on startup with --update-cleanup."""
    time.sleep(2)

    old = _old_path()
    marker = _rollback_marker_path()

    if old and old.exists():
        try:
            old.unlink()
        except PermissionError:
            time.sleep(3)
            try:
                old.unlink()
            except Exception:
                pass

    if marker and marker.exists():
        try:
            marker.unlink()
        except Exception:
            pass

    try:
        from updater import cleanup_download
        cleanup_download()
    except Exception:
        pass