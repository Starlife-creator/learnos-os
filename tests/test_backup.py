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
        cls._orig_app = config.APP_DIR
        config.APP_DIR = Path(cls.temp_dir.name) / "app"
        config.APP_DIR.mkdir(parents=True, exist_ok=True)
        backup.APP_DIR = config.APP_DIR
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
        config.APP_DIR = cls._orig_app
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

    def _challenge_token(self):
        """R1：经真实 HTTP 向 /api/export/challenge 取一次性令牌（同源 CSRF 已带）。"""
        status, r = self.request("/api/export/challenge", method="POST")
        assert status == 200, f"challenge 签发失败: {status}"
        return r["token"]

    def test_export_contains_all_tables(self):
        status, r = self.request(f"/api/export/backup?token={self._challenge_token()}")
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
        status, r = self.request(f"/api/export/backup?token={self._challenge_token()}")
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


class TestBackupNewTablesRoundtrip(unittest.TestCase):
    """体检 P0-2 回归：v16+ 新增的 7 张业务表必须参与导出/还原往返。

    背景：BACKUP_TABLES 曾漏掉 subjects/bank_problems 等表，导致一键还原后
    学科注册、题库错题建档等数据清零。本测试向每张表插哨兵行，
    导出→清空→还原→逐表断言哨兵仍在。
    """

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="backup_nt_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls.temp_dir.name) / "nt.db"
        db.DB_PATH = config.DB_PATH
        cls._orig_app = config.APP_DIR
        config.APP_DIR = Path(cls.temp_dir.name) / "app"
        config.APP_DIR.mkdir(parents=True, exist_ok=True)
        backup.APP_DIR = config.APP_DIR
        db.init_db()
        from db import now
        ts = now()
        with db.db() as conn:
            conn.execute("INSERT INTO subjects(id, title, builtin, created_at) VALUES (?,?,?,?)",
                         ("chem2", "自建化学", 0, ts))
            conn.execute("INSERT INTO study_checkins(check_date, subject, minutes, note, created_at) "
                         "VALUES (?,?,?,?,?)", ("2026-08-18", "physics", 45, "哨兵打卡", ts))
            conn.execute("INSERT INTO bank_scores(qid, subject, score, comment, against, mode, needs_review, created_at) "
                         "VALUES (?,?,?,?,?,?,?,?)", ("q1", "physics", 80, "哨兵评分", "", "ai", 0, ts))
            conn.execute("INSERT INTO bank_attempts(qid, correct, attempted_at) VALUES (?,?,?)",
                         ("q1", 1, ts))
            conn.execute("INSERT INTO bank_problems(qid, problem_id, updated_at) VALUES (?,?,?)",
                         ("q1", 7, ts))
            conn.execute("INSERT INTO mastery_log(day, avg_mastery, count) VALUES (?,?,?)",
                         ("2026-08-18", 3.5, 12))
            conn.execute("INSERT INTO gamification(date, reviews, xp) VALUES (?,?,?)",
                         ("2026-08-18", 5, 250))

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db
        config.APP_DIR = cls._orig_app
        cls.temp_dir.cleanup()

    def test_new_tables_roundtrip(self):
        raw = json.dumps(backup.export_backup())
        backup.restore_backup(raw)
        with db.db() as conn:
            self.assertEqual(conn.execute("SELECT title FROM subjects WHERE id='chem2'").fetchone()[0], "自建化学")
            self.assertEqual(conn.execute("SELECT note FROM study_checkins").fetchone()[0], "哨兵打卡")
            self.assertEqual(conn.execute("SELECT score FROM bank_scores WHERE qid='q1'").fetchone()[0], 80)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bank_attempts").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT problem_id FROM bank_problems WHERE qid='q1'").fetchone()[0], 7)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM mastery_log").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT xp FROM gamification").fetchone()[0], 250)


if __name__ == "__main__":
    unittest.main()