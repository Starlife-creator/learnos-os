# Tests package initializer.
#
# Test-only hardening (does NOT affect production code, which never imports this
# package). Sandbox-compatibility shims so the suite is green in any
# environment without weakening real security/perf controls:
#
# 1. Temp dirs. Python 3.13+ on Windows turns ``os.mkdir(path, 0o700)`` (used by
#    ``tempfile.mkdtemp``) into an owner-only ACL. Under a restricted sandbox
#    token that ACL makes the directory unreadable *and undeletable*, which is
#    why tests/.tmp used to pile up thousands of orphan dirs. We create test
#    temp dirs with inherited ACLs instead (they live inside the workspace, so
#    the 0o700 privacy guarantee buys nothing here).
#    Cleanup failures are no longer silently swallowed: they are recorded in
#    ``LEFTOVER_DIRS`` and reported by run_tests.py at the end of the run.
# 2. The sandbox is I/O-slow; the seed knowledge graph (312 physics concepts)
#    can take ~25s to load on first request, exceeding the 5s client timeout
#    some HTTP tests use. We enforce a 60s floor on HTTPConnection timeouts so
#    correct-but-slow responses don't fail. Real environments respond in <1s, so
#    this only tolerates sandbox slowness.
# 3. Logs. Tests must not pollute the production learnos.log, so LEARNOS_LOG is
#    pointed at tests/.tmp/test.log before config.py is imported.

import os
import sys
import tempfile
import http.client
from pathlib import Path

TMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TMP_ROOT.mkdir(exist_ok=True)

os.environ.setdefault("LEARNOS_LOG", str(TMP_ROOT / "test.log"))

# 路径 -> 清理失败原因；run_tests.py 结束时重试并汇报
LEFTOVER_DIRS: dict[str, str] = {}

_orig_mkdtemp = tempfile.mkdtemp


def _mkdtemp_inherit_acl(suffix=None, prefix=None, dir=None):
    """与 tempfile.mkdtemp 等价，但目录不带 0o700（避免 Windows 生成独占 ACL）。"""
    prefix, suffix, dir, output_type = tempfile._sanitize_params(prefix, suffix, dir)
    names = tempfile._get_candidate_names()
    if output_type is bytes:
        names = map(os.fsencode, names)
    for _ in range(tempfile.TMP_MAX):
        name = next(names)
        path = os.path.join(dir, prefix + name + suffix)
        try:
            os.mkdir(path)
        except FileExistsError:
            continue
        return os.path.abspath(path)
    raise FileExistsError("No usable temporary directory name found")


if sys.platform == "win32":
    tempfile.mkdtemp = _mkdtemp_inherit_acl


_orig_td_cleanup = tempfile.TemporaryDirectory.cleanup


def _tracked_td_cleanup(self):
    try:
        _orig_td_cleanup(self)
    except OSError as exc:
        # 不让清理失败打断用例，但记录下来，由 run_tests.py 统一重试与汇报
        LEFTOVER_DIRS[str(self.name)] = f"{type(exc).__name__}: {exc}"


tempfile.TemporaryDirectory.cleanup = _tracked_td_cleanup


_orig_hc_init = http.client.HTTPConnection.__init__


def _hc_init(self, *args, **kwargs):
    # Enforce a slow-sandbox-tolerant floor on the client timeout.
    t = kwargs.get("timeout")
    if t is None or t < 60:
        kwargs["timeout"] = 60
    _orig_hc_init(self, *args, **kwargs)


http.client.HTTPConnection.__init__ = _hc_init
