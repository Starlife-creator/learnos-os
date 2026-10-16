"""Run the LearnOS test suite.

Use package-mode discovery (`-t .`) so that ``tests/__init__.py`` hardening
patches are applied (sandbox-safe temp dirs, test log redirection, HTTP client
timeout floor). Without package mode, ``unittest discover -s tests`` imports
test modules top-level and skips the package initializer.

After the run, temp dirs whose cleanup failed are retried once and any that
still remain are reported, so leftovers in tests/.tmp never pile up silently.

Usage:
    python run_tests.py                 # full suite
    python run_tests.py tests.test_db   # a single module
"""
import gc
import shutil
import sys
import unittest
from pathlib import Path


def _retry_leftovers() -> int:
    """重试删除清理失败的临时目录，返回仍残留的数量。"""
    try:
        import tests
    except ImportError:
        return 0
    gc.collect()  # 释放未显式关闭的 sqlite 连接句柄
    try:
        import db
        db.close_all_connections()
    except Exception:
        pass
    remaining = {}
    for path, reason in tests.LEFTOVER_DIRS.items():
        if not Path(path).exists():
            continue
        try:
            shutil.rmtree(path)
        except OSError as exc:
            remaining[path] = f"{type(exc).__name__}: {exc}"
    if remaining:
        print(f"\n[run_tests] {len(remaining)} temp dir(s) could not be removed:", file=sys.stderr)
        for path, reason in list(remaining.items())[:10]:
            print(f"  {path} -> {reason}", file=sys.stderr)
    return len(remaining)


if __name__ == "__main__":
    argv = ["unittest"]
    if len(sys.argv) > 1:
        argv += sys.argv[1:]
    else:
        argv += ["discover", "-s", "tests", "-t", ".", "-p", "test_*.py"]
    prog = unittest.main(module=None, argv=argv, exit=False)
    _retry_leftovers()
    sys.exit(0 if prog.result.wasSuccessful() else 1)
