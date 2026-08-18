"""Tier A 回归测试：P1（AI 结果缓存 TTL）/ P2（restore 强鉴权）。

随 Tier A 落地新增，守护两项关键修复不被回归：
- P1：`_RESULT_CACHE_TTL` 退化为 30 秒会使缓存形同虚设、悄悄推高 token。
- P2：`/api/import/restore` 不再校验导出令牌（整库重建鉴权弱于只读导出）已闭合。
"""
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
import ai
import backup
import handler_problems
from handler_problems import ProblemsMixin

_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)


class _FakeHandler(ProblemsMixin):
    """最小 handler 替身：仅实现 P2 测试所需的方法/属性。"""

    def __init__(self, token_header: str = ""):
        self.path = "/api/import/restore"
        self.headers: dict[str, str] = {}
        if token_header:
            self.headers["X-Export-Token"] = token_header
        self.status = None
        self.body = None

    def json_response(self, data, status: int = 200):
        self.status = status
        self.body = data


class TestTierA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="tiera_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls.temp_dir.name) / "tiera.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()
        # ai_result_cache 表在 ai 导入时已对（当时的）真实 DB 建过；此处对临时 DB 补建
        ai._ensure_cache_table()
        cls._orig_app = config.APP_DIR
        config.APP_DIR = Path(cls.temp_dir.name) / "app"
        config.APP_DIR.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        config.DB_PATH = cls._orig_db
        config.APP_DIR = cls._orig_app
        cls.temp_dir.cleanup()

    # ── P1：缓存 TTL 绝不能退化成 30 秒 ──
    def test_cache_ttl_is_thirty_days(self):
        # 直接锁定常量：任何把 _RESULT_CACHE_TTL 改回 30 的回归都会立刻失败
        self.assertGreaterEqual(ai._RESULT_CACHE_TTL, 30 * 24 * 3600 - 1)

    def test_cache_roundtrip(self):
        key = f"tiera_{time.time()}"
        ai.cache_set(key, {"v": 42})
        self.assertEqual(ai.cache_get(key), {"v": 42})

    # ── P2：restore 必须校验导出令牌 ──
    def test_restore_requires_export_token(self):
        called = {}

        def fake_restore(raw):
            called["x"] = True
            return {"restored": {}}

        # 无令牌 → 401 且根本不应进入 restore_backup
        fh = _FakeHandler(token_header="")
        with unittest.mock.patch.object(backup, "restore_backup", fake_restore):
            fh._handle_backup_restore({"backup": "{}"})
        self.assertEqual(fh.status, 401)
        self.assertNotIn("x", called)

        # 有效令牌 → 放行并调用 restore_backup
        fh2 = _FakeHandler(token_header=config.EXPORT_TOKEN)
        with unittest.mock.patch.object(backup, "restore_backup", fake_restore):
            fh2._handle_backup_restore({"backup": "{}"})
        self.assertEqual(fh2.status, 200)
        self.assertIn("x", called)


if __name__ == "__main__":
    unittest.main()
