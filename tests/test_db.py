"""测试数据库层和 settings_dict 密钥遮蔽。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db


class TestDatabase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix="db_")
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmpdir) / "db_test.db"
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        config.DB_PATH = cls._orig_db

    def test_schema_created(self):
        tables = db.rows("SELECT name FROM sqlite_master WHERE type='table'")
        names = {t["name"] for t in tables}
        self.assertIn("problems", names)
        self.assertIn("hints", names)
        self.assertIn("reviews", names)
        self.assertIn("oral_sessions", names)
        self.assertIn("settings", names)

    def test_default_settings_inserted(self):
        s = db.settings_dict(include_secret=True)
        for key in ("api_base", "api_key", "model", "temperature"):
            self.assertIn(key, s)

    def test_insert_and_fetch_problem(self):
        from datetime import datetime
        stamp = datetime.now().isoformat(timespec="seconds")
        with db.db() as conn:
            cursor = conn.execute(
                "INSERT INTO problems(title, course, topic, content, mastery, ease_factor, repetition, created_at, updated_at) VALUES (?,?,?,?,2,2.5,0,?,?)",
                ("测试题", "力学", "牛顿定律", "求加速度", stamp, stamp),
            )
            pid = cursor.lastrowid
        r = db.row("SELECT * FROM problems WHERE id = ?", (pid,))
        self.assertIsNotNone(r)
        self.assertEqual(r["title"], "测试题")
        self.assertEqual(r["ease_factor"], 2.5)

    def test_cascade_delete(self):
        from datetime import datetime
        stamp = datetime.now().isoformat(timespec="seconds")
        with db.db() as conn:
            cursor = conn.execute(
                "INSERT INTO problems(title, content, mastery, ease_factor, repetition, created_at, updated_at) VALUES (?,?,1,2.5,0,?,?)",
                ("级联测试", "content", stamp, stamp),
            )
            pid = cursor.lastrowid
            conn.execute(
                "INSERT INTO hints(problem_id, level, content, created_at) VALUES (?,1,'hint',?)",
                (pid, stamp),
            )
            conn.execute(
                "INSERT INTO reviews(problem_id, due_date, interval_days, created_at) VALUES (?,?,1,?)",
                (pid, stamp, stamp),
            )

        with db.db() as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM problems WHERE id = ?", (pid,))

        hints = db.rows("SELECT * FROM hints WHERE problem_id = ?", (pid,))
        reviews = db.rows("SELECT * FROM reviews WHERE problem_id = ?", (pid,))
        self.assertEqual(len(hints), 0)
        self.assertEqual(len(reviews), 0)

    def test_schema_version_table_exists(self):
        """迁移后应存在 schema_version 表。"""
        tables = db.rows("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'")
        self.assertEqual(len(tables), 1)
        versions = db.rows("SELECT version FROM schema_version")
        self.assertTrue(any(v["version"] == 1 for v in versions))


class TestSettingsDict(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix="settings_")
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmpdir) / "settings_test.db"
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        config.DB_PATH = cls._orig_db
        os.environ.pop("PHYSICS_OS_API_KEY", None)

    def test_secret_masked_by_default(self):
        with db.db() as conn:
            conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('api_key', 'sk-secret123')")
        s = db.settings_dict()
        self.assertEqual(s["api_key"], "••••••••")
        self.assertTrue(s["has_api_key"])

    def test_secret_visible_with_flag(self):
        s = db.settings_dict(include_secret=True)
        self.assertEqual(s["api_key"], "sk-secret123")

    def test_env_key_overrides(self):
        os.environ["PHYSICS_OS_API_KEY"] = "sk-from-env"
        s = db.settings_dict(include_secret=True)
        self.assertEqual(s["api_key"], "sk-from-env")
        self.assertEqual(s["key_source"], "environment")
        os.environ.pop("PHYSICS_OS_API_KEY", None)

    def test_no_key_returns_empty(self):
        with db.db() as conn:
            conn.execute("DELETE FROM settings WHERE key = 'api_key'")
        os.environ.pop("PHYSICS_OS_API_KEY", None)
        s = db.settings_dict()
        self.assertFalse(s["has_api_key"])
        self.assertEqual(s["api_key"], "")


if __name__ == "__main__":
    unittest.main()
