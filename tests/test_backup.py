"""B5 一键备份/还原测试。"""
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
import backup
from handler import Handler

_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)


class TestBackup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="backup_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls.temp_dir.name) / "backup_test.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()
        with db.db() as conn:
            from db import now
            conn.execute(
                "INSERT INTO problems(title, course, topic, content, mastery, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("备份题", "光学", "光的折射", "内容", 3, now(), now()),
            )
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db
        cls.temp_dir.cleanup()

    def request(self, path, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "X-Requested-With": "LearnOS"},
        )
        with urllib.request.urlopen(req, timeout=8) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_export_contains_all_tables(self):
        status, r = self.request(f"/api/export/backup?token={config.EXPORT_TOKEN}")
        self.assertEqual(status, 200)
        self.assertIn("problems", r["tables"])
        self.assertIn("exam_papers", r["tables"])
        self.assertIn("settings", r["tables"])
        self.assertEqual(len(r["tables"]["problems"]), 1)
        self.assertEqual(r["tables"]["problems"][0]["title"], "备份题")

    def test_restore_roundtrip(self):
        # 再增一条，导出
        with db.db() as conn:
            from db import now
            conn.execute(
                "INSERT INTO problems(title, course, topic, content, mastery, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("第二条", "力学", "动能定理", "内容", 2, now(), now()),
            )
        status, r = self.request(f"/api/export/backup?token={config.EXPORT_TOKEN}")
        raw = json.dumps(r)

        # 清空库再还原
        backup.restore_backup(raw)
        with db.db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM problems").fetchone()[0]
            titles = [x[0] for x in conn.execute("SELECT title FROM problems ORDER BY id")]
        self.assertEqual(count, 2)
        self.assertEqual(titles, ["备份题", "第二条"])
        # 工作区内生成 .bak
        baks = list(Path(self.temp_dir.name).glob("*.bak"))
        self.assertEqual(len(baks), 1)
        self.assertIn("restore", baks[0].name)

    def test_restore_invalid(self):
        with self.assertRaises(ValueError):
            backup.restore_backup("not json")
        with self.assertRaises(ValueError):
            backup.restore_backup(json.dumps({"version": 99}))

    def test_auto_backup_idempotent_and_prune(self):
        """C7：自动备份每天一次幂等，且最多保留 7 份。"""
        from datetime import date as _date
        from config import APP_DIR
        backups_dir = APP_DIR / "backups"
        # 清掉当日已有备份，避免幂等跳过导致环境依赖失败
        for f in backups_dir.glob(f"auto_{_date.today().isoformat()}_*.db"):
            f.unlink()
        # 预置 8 个陈旧备份模拟累积
        backups_dir.mkdir(parents=True, exist_ok=True)
        for i in range(8):
            (backups_dir / f"auto_2026-01-{(i % 28) + 1:02d}_{i:06d}.db").write_bytes(b"x")
        first = backup.auto_backup_if_due()
        self.assertIsNotNone(first)
        self.assertTrue(first.exists())
        # 当日再调 → 幂等跳过
        self.assertIsNone(backup.auto_backup_if_due())
        # 陈旧备份被裁剪到 7 份以内
        remaining = list(backups_dir.glob("auto_*.db"))
        self.assertLessEqual(len(remaining), 7)
        for f in remaining:
            if f != first:
                f.unlink()
        first.unlink()


if __name__ == "__main__":
    unittest.main()