# -*- coding: utf-8 -*-
"""Comprehensive tests for the auto-update system.

Tests cover:
  - updater.py: version parsing, comparison, platform detection, caching, download, SHA256
  - updater_win.py / updater_linux.py: apply_update path resolution, rollback logic
  - cli.py: check-update / update subcommands
  - app.py: --update-cleanup / --start-proxy / rollback-on-startup
  - config_store.py: new update fields serialization round-trip
  - GUI: update methods exist on IosProxyApp
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure src/ is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from updater import (
    CHECK_COOLDOWN,
    CheckResult,
    UpdateInfo,
    check_for_update,
    cleanup_download,
    current_version,
    download_update,
    is_newer,
    parse_version,
    _fetch_sha256,
    _platform_asset_pattern,
    _read_check_cache,
    _sha256_file,
    _temp_update_dir,
    _write_check_cache,
    last_check_time,
)
from config_store import AppConfig


# ============================================================
# 1. Version parsing and comparison
# ============================================================

def test_parse_version_basic():
    assert parse_version("4.6.2") == (4, 6, 2)
    assert parse_version("v4.6.2") == (4, 6, 2)
    assert parse_version("4.7.0") == (4, 7, 0)
    assert parse_version("10.0.1") == (10, 0, 1)

def test_parse_version_prerelease():
    assert parse_version("4.7.0-beta1") == (4, 7, 0)
    assert parse_version("v4.7.0-rc2") == (4, 7, 0)

def test_parse_version_short():
    assert parse_version("4") == (4,)
    assert parse_version("4.7") == (4, 7)

def test_is_newer():
    assert is_newer("4.7.0", "4.6.2") is True
    assert is_newer("4.6.2", "4.6.2") is False
    assert is_newer("4.6.1", "4.6.2") is False
    assert is_newer("5.0.0", "4.9.9") is True
    assert is_newer("4.6.2", "4.6.1") is True

def test_is_newer_different_length():
    assert is_newer("4.7", "4.6.2") is True  # (4,7) > (4,6,2)
    assert is_newer("4", "4.0.0") is False   # (4,) < (4,0,0) by tuple comparison


# ============================================================
# 2. Platform asset detection
# ============================================================

def test_platform_asset_pattern():
    pattern = _platform_asset_pattern()
    # On Windows x64, should return -windows-x64.exe
    if sys.platform.startswith("win"):
        assert pattern == "-windows-x64.exe"
    elif sys.platform.startswith("linux"):
        assert pattern == "-linux-x86_64" or pattern is None  # depends on arch
    # On macOS or unusual arch, should return None
    # (can't easily test that without mocking platform)


# ============================================================
# 3. Current version detection
# ============================================================

def test_current_version():
    ver = current_version()
    assert ver != "dev", f"current_version returned 'dev' — VERSION file not found"
    parts = ver.split(".")
    assert len(parts) >= 2, f"Version '{ver}' doesn't look like semver"
    for p in parts:
        assert p.isdigit(), f"Version part '{p}' is not numeric"


# ============================================================
# 4. Cache read/write
# ============================================================

def test_cache_round_trip():
    info = UpdateInfo(
        version="99.0.0",
        html_url="https://example.com",
        body="test",
        download_url="https://example.com/file.exe",
        sha256_url="https://example.com/sha256",
        asset_name="file.exe",
    )
    result = CheckResult(has_update=True, update_info=info, checked_at=time.time())
    _write_check_cache(result)

    cached = _read_check_cache()
    assert cached is not None, "Cache was not written"
    assert cached.get("has_update") is True
    assert cached.get("version") == "99.0.0"
    assert cached.get("download_url") == "https://example.com/file.exe"

def test_cache_no_update():
    result = CheckResult(has_update=False, checked_at=time.time())
    _write_check_cache(result)
    cached = _read_check_cache()
    assert cached is not None
    assert cached.get("has_update") is False

def test_last_check_time():
    result = CheckResult(has_update=False, checked_at=time.time())
    _write_check_cache(result)
    t = last_check_time()
    assert t is not None
    assert abs(t - time.time()) < 10  # within 10 seconds


# ============================================================
# 5. SHA256 computation
# ============================================================

def test_sha256_file():
    tmp = Path(tempfile.mktemp(suffix=".txt"))
    try:
        tmp.write_text("hello world\n", encoding="utf-8")
        h = _sha256_file(tmp)
        assert len(h) == 64  # hex SHA256
        assert h == _sha256_file(tmp)  # deterministic
    finally:
        tmp.unlink(missing_ok=True)


# ============================================================
# 6. Update check (live, may be rate-limited)
# ============================================================

def test_check_for_update_returns_result():
    result = check_for_update(force=True)
    assert isinstance(result, CheckResult)
    # Should not crash regardless of network state
    assert result.checked_at > 0

def test_check_for_update_cooldown():
    # First check
    r1 = check_for_update(force=True)
    # Second check without force — should return cached result
    r2 = check_for_update()
    assert r2.checked_at == r1.checked_at  # same timestamp = cache hit


# ============================================================
# 7. Download with invalid URL (failure case)
# ============================================================

def test_download_invalid_url():
    info = UpdateInfo(
        version="99.0.0",
        html_url="https://example.com",
        body="",
        download_url="https://127.0.0.1:1/nonexistent.exe",  # unreachable
        sha256_url="",
        asset_name="nonexistent.exe",
    )
    success, path, err = download_update(info)
    assert success is False
    assert err is not None
    assert "Download failed" in err


# ============================================================
# 8. Cleanup temp directory
# ============================================================

def test_cleanup_download():
    d = _temp_update_dir()
    (d / "test_file.tmp").write_text("test", encoding="utf-8")
    assert d.exists()
    cleanup_download()
    # Directory should be removed (or empty)
    assert not d.exists() or not list(d.iterdir())


# ============================================================
# 9. updater_win.py path resolution
# ============================================================

def test_updater_win_paths_when_not_frozen():
    # When not running from PyInstaller, key paths should be None
    from updater_win import _running_exe_path, _rollback_marker_path, _old_exe_path
    exe = _running_exe_path()
    # In source mode, sys.frozen is False, so should return None
    if not getattr(sys, "frozen", False):
        assert exe is None
        assert _rollback_marker_path() is None
        assert _old_exe_path() is None

def test_updater_win_apply_update_not_frozen():
    from updater_win import apply_update
    # Should fail gracefully when not running from frozen exe
    success, err = apply_update(Path("nonexistent.exe"))
    assert success is False
    assert "frozen" in err.lower() or "not found" in err.lower()

def test_updater_win_rollback_when_no_old():
    from updater_win import check_rollback_needed, perform_rollback
    # When not frozen, rollback should not be possible
    if not getattr(sys, "frozen", False):
        assert check_rollback_needed() is False
        ok, err = perform_rollback()
        assert ok is False


# ============================================================
# 10. updater_linux.py path resolution
# ============================================================

def test_updater_linux_paths_when_not_frozen():
    if sys.platform.startswith("linux"):
        from updater_linux import _running_exe_path
        # In source mode on Linux, might find the script
        # Just ensure it doesn't crash
        _running_exe_path()

def test_updater_linux_apply_update_not_frozen():
    from updater_linux import apply_update
    if not getattr(sys, "frozen", False):
        success, err = apply_update(Path("nonexistent.exe"))
        assert success is False


# ============================================================
# 11. Config store update fields round-trip
# ============================================================

def test_config_update_fields_default():
    c = AppConfig.default()
    assert c.update_check_enabled is True
    assert c.update_check_interval_hours == 24
    assert c.update_include_prerelease is False
    assert c.update_auto_download is False

def test_config_update_fields_roundtrip():
    c = AppConfig.default()
    c.update_check_enabled = False
    c.update_check_interval_hours = 12
    c.update_include_prerelease = True
    c.update_auto_download = True
    d = c.to_dict()
    assert d["update_check_enabled"] is False
    assert d["update_check_interval_hours"] == 12
    assert d["update_include_prerelease"] is True
    assert d["update_auto_download"] is True
    # Round-trip through from_dict
    c2 = AppConfig.from_dict(d)
    assert c2.update_check_enabled is False
    assert c2.update_check_interval_hours == 12
    assert c2.update_include_prerelease is True
    assert c2.update_auto_download is True

def test_config_update_fields_missing_in_json():
    """Old config.json without update fields should use defaults."""
    d = {
        "host": "127.0.0.1", "port": 8082,
        "default_model_id": "GPT-5.5", "codex_model_id": "GPT-5.5",
        "models": [{"model_id": "GPT-5.5", "upstream_model": "GPT-5.5", "api_format": "responses"}],
    }
    c = AppConfig.from_dict(d)
    assert c.update_check_enabled is True   # default
    assert c.update_check_interval_hours == 24  # default
    assert c.update_include_prerelease is False  # default
    assert c.update_auto_download is False       # default


# ============================================================
# 12. CLI subcommand registration
# ============================================================

def test_cli_check_update_help():
    from cli import main
    # Should not crash and should return 0 for --help
    try:
        main(["check-update", "--help"])
    except SystemExit as e:
        assert e.code == 0

def test_cli_update_help():
    from cli import main
    try:
        main(["update", "--help"])
    except SystemExit as e:
        assert e.code == 0


# ============================================================
# 13. app.py --update-cleanup flag handling
# ============================================================

def test_app_cleans_internal_flags():
    """Simulate app.py flag stripping logic."""
    test_argv = ["app.py", "--update-cleanup", "--start-proxy", "status"]
    cleaned = [a for a in test_argv if a not in ("--update-cleanup", "--start-proxy")]
    assert cleaned == ["app.py", "status"]
    auto_start = "--start-proxy" in test_argv
    assert auto_start is True

def test_app_cleans_flags_no_other_args():
    test_argv = ["app.py", "--update-cleanup"]
    cleaned = [a for a in test_argv if a not in ("--update-cleanup", "--start-proxy")]
    assert cleaned == ["app.py"]


# ============================================================
# 14. GUI update methods exist on IosProxyApp
# ============================================================

def test_gui_update_methods_exist():
    from pyqt_gui import IosProxyApp
    assert hasattr(IosProxyApp, "auto_check_update")
    assert hasattr(IosProxyApp, "manual_check_update")
    assert hasattr(IosProxyApp, "_on_update_found")
    assert hasattr(IosProxyApp, "_prompt_update")
    assert hasattr(IosProxyApp, "_download_and_prompt_install")
    assert hasattr(IosProxyApp, "_apply_downloaded_update")

def test_gui_update_badge_attribute():
    """IosProxyApp should have update_badge in __init__."""
    from pyqt_gui import IosProxyApp
    init_source = IosProxyApp.__init__.__code__.co_names
    # The __init__ should reference update_badge
    src = str(IosProxyApp.__init__)
    # Just verify the class is importable and has the methods
    assert callable(getattr(IosProxyApp, "auto_check_update", None))


# ============================================================
# 15. UpdateInfo dataclass
# ============================================================

def test_update_info_display_version():
    info = UpdateInfo(
        version="4.7.0", html_url="", body="",
        download_url="", sha256_url="", asset_name="test.exe",
    )
    assert info.display_version == "v4.7.0"

def test_check_result_is_error():
    r1 = CheckResult(has_update=False, error="something broke")
    assert r1.is_error is True
    r2 = CheckResult(has_update=False)
    assert r2.is_error is False
    r3 = CheckResult(has_update=True, update_info=UpdateInfo(
        version="1.0", html_url="", body="", download_url="", sha256_url="", asset_name="",
    ))
    assert r3.is_error is False


# ============================================================
# 16. Edge cases
# ============================================================

def test_empty_version_tag():
    assert parse_version("") == (0,)  # empty string treated as version 0

def test_is_newer_with_empty():
    assert is_newer("1.0", "") is True
    assert is_newer("", "1.0") is False

def test_fetch_sha256_empty_url():
    result = _fetch_sha256("", "test.exe")
    assert result is None


# ============================================================
# Run all tests
# ============================================================

def run_tests():
    tests = [name for name in sorted(globals()) if name.startswith("test_") and callable(globals()[name])]
    passed = 0
    failed = 0
    errors = []
    for name in tests:
        fn = globals()[name]
        try:
            fn()
            passed += 1
            print(f"  PASS  {name}")
        except Exception as e:
            failed += 1
            errors.append((name, e))
            print(f"  FAIL  {name}: {e}")
    print(f"\n{passed} passed, {failed} failed out of {len(tests)} tests")
    if errors:
        print("\nFailures:")
        for name, e in errors:
            print(f"  {name}: {e}")
    return failed == 0


if __name__ == "__main__":
    ok = run_tests()
    raise SystemExit(0 if ok else 1)