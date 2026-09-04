"""测试数据库层和 settings_dict 密钥遮蔽。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db

# 测试临时数据严格限制在工作区内（tests/.tmp/），不留任何外部痕迹
_TEST_TMP_DIR = Path(__file__).resolve().parent / ".tmp"
_TEST_TMP_DIR.mkdir(exist_ok=True)


class TestDatabase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="db_", dir=_TEST_TMP_DIR)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "db_test.db"
        db.DB_PATH = config.DB_PATH  # db 模块按值绑定，需同步替换
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()
        db.DB_PATH = cls._orig_db
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
        # v32（D1 复习日志前后快照）已应用且为当前最高版本
        top = db.row("SELECT MAX(version) AS v FROM schema_version")["v"]
        self.assertGreaterEqual(int(top), 32)
        self.assertIn(32, [int(v["version"]) for v in versions])


class TestV32MigrationReplay(unittest.TestCase):
    """D1 迁移回放：构造 v31 旧库（card_reviews 旧列 + 存量评分行）→ 升级到 v32。

    验收：新列齐全、存量行零损且落入默认值（prev_due='' → 不可撤销语义）、
    schema_version 前进到 32、旧代码写入路径（不带新列的 INSERT）仍可跑。
    """

    def test_v31_to_v32_upgrade(self):
        import sqlite3
        with tempfile.TemporaryDirectory(prefix="db_v32_", dir=_TEST_TMP_DIR) as td:
            old = Path(td) / "v31.db"
            # 手工构造 v31 状态库：基础表 + schema_version 到 31 + 旧结构评分行
            conn = sqlite3.connect(old)
            conn.executescript("""
                CREATE TABLE cards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL DEFAULT 'physics',
                    concept_id INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL DEFAULT 'qa',
                    cue TEXT NOT NULL, answer TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
                    source TEXT NOT NULL DEFAULT 'manual', created_at TEXT NOT NULL,
                    ease_factor REAL NOT NULL DEFAULT 2.5, repetition INTEGER NOT NULL DEFAULT 0,
                    state INTEGER NOT NULL DEFAULT 0, stability REAL NOT NULL DEFAULT 0.0,
                    difficulty REAL NOT NULL DEFAULT 0.0, due_date TEXT NOT NULL DEFAULT '',
                    interval_days INTEGER NOT NULL DEFAULT 1, last_review TEXT NOT NULL DEFAULT '');
                CREATE TABLE card_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, card_id INTEGER NOT NULL,
                    due_date TEXT NOT NULL DEFAULT '', interval_days INTEGER NOT NULL DEFAULT 1,
                    rating INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                    FOREIGN KEY(card_id) REFERENCES cards(id) ON DELETE CASCADE);
                CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
                CREATE TABLE subjects (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
                    builtin INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
                CREATE TABLE seed_versions (
                    subject TEXT PRIMARY KEY, seed_version INTEGER NOT NULL DEFAULT 0,
                    applied_at TEXT NOT NULL);
                INSERT INTO cards(id, cue, created_at, due_date) VALUES (1, '旧卡', '2026-01-01T00:00:00', '2026-01-02');
                INSERT INTO card_reviews(card_id, due_date, interval_days, rating, created_at)
                    VALUES (1, '2026-01-03', 2, 3, '2026-01-02T10:00:00');
                INSERT INTO schema_version(version, applied_at) VALUES (25, '2026-01-01T00:00:00');
                INSERT INTO schema_version(version, applied_at) VALUES (31, '2026-01-01T00:00:00');
            """)
            conn.commit()
            conn.close()
            # 走正式迁移路径升级
            orig = config.DB_PATH
            config.DB_PATH = old
            db.DB_PATH = old
            try:
                db.init_db()
                cols = {r["name"] for r in db.rows("PRAGMA table_info(card_reviews)")}
                for c in ("prev_state", "prev_stability", "prev_difficulty", "prev_due",
                          "prev_interval", "prev_repetition", "prev_ease", "prev_last_review",
                          "cur_state", "cur_stability", "cur_difficulty",
                          "fsrs_params_version", "undone"):
                    self.assertIn(c, cols, f"v32 应新增列 {c}")
                legacy = db.row("SELECT * FROM card_reviews WHERE card_id = 1")
                self.assertEqual(legacy["rating"], 3, "存量评分行必须零损")
                self.assertEqual(legacy["prev_due"], "", "旧行 prev_due 落默认空串（不可撤销语义）")
                self.assertEqual(legacy["undone"], 0)
                top = db.row("SELECT MAX(version) AS v FROM schema_version")["v"]
                self.assertGreaterEqual(int(top), 32)
                # 旧代码写入路径兼容：不带新列的 INSERT 仍可执行（回滚=撤代码不撤库）
                with db.DB_LOCK, db.db() as c2:
                    c2.execute(
                        "INSERT INTO card_reviews(card_id, due_date, interval_days, rating, created_at) "
                        "VALUES (1, '2026-01-05', 3, 4, '2026-01-04T10:00:00')")
                self.assertEqual(len(db.rows("SELECT id FROM card_reviews WHERE card_id = 1")), 2)
            finally:
                db.close_all_connections()  # 释放临时库句柄（Windows 文件锁）
                config.DB_PATH = orig
                db.DB_PATH = orig


class TestV35MigrationReplay(unittest.TestCase):
    """B4-1 G1/G2 迁移回放：构造 v34 旧库（旧结构 concept_links/concepts + 存量边/概念）
    → 升级到 v35。

    验收：溯源列齐全（strength/reason/evidence_ref + evidence/assessment_prompt）、
    存量行零损且落入默认（soft/空串）、schema_version 前进到 35、
    旧代码写入路径（不带新列的 INSERT）仍可跑（回滚=撤代码不撤库）。
    """

    def test_v34_to_v35_upgrade(self):
        import sqlite3
        with tempfile.TemporaryDirectory(prefix="db_v35_", dir=_TEST_TMP_DIR) as td:
            old = Path(td) / "v34.db"
            # 手工构造 v34 状态库：旧结构 concept_links（3 列）+ concepts（无判据/口试列）
            conn = sqlite3.connect(old)
            conn.executescript("""
                CREATE TABLE concept_links (
                    concept_a INTEGER NOT NULL,
                    concept_b INTEGER NOT NULL,
                    relation TEXT NOT NULL CHECK (relation IN
                        ('prerequisite','related','contrast','analogy','inclusion','progression')),
                    PRIMARY KEY (concept_a, concept_b, relation)
                );
                CREATE TABLE concepts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    parent_id INTEGER NOT NULL DEFAULT 0,
                    chapter_id INTEGER NOT NULL DEFAULT 0,
                    difficulty REAL NOT NULL DEFAULT 0.5,
                    mastery_est REAL NOT NULL DEFAULT 0.0,
                    looms_in INTEGER NOT NULL DEFAULT 0,
                    user_edited INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    subject TEXT NOT NULL DEFAULT 'physics',
                    aliases TEXT NOT NULL DEFAULT '',
                    explanation TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'unknown',
                    explanation_seed TEXT NOT NULL DEFAULT '',
                    explanation_user TEXT
                );
                CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
                CREATE TABLE subjects (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
                    builtin INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
                CREATE TABLE seed_versions (
                    subject TEXT PRIMARY KEY, seed_version INTEGER NOT NULL DEFAULT 0,
                    applied_at TEXT NOT NULL);
                CREATE TABLE trash (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                    entity_id INTEGER NOT NULL, payload_json TEXT NOT NULL,
                    trashed_at TEXT NOT NULL, restored_at TEXT NOT NULL DEFAULT '');
                INSERT INTO concepts(id, name, created_at, subject) VALUES
                    (1, '牛顿第二定律', '2026-01-01T00:00:00', 'physics'),
                    (2, '简谐运动', '2026-01-01T00:00:00', 'physics');
                INSERT INTO concept_links(concept_a, concept_b, relation)
                    VALUES (1, 2, 'prerequisite');
                INSERT INTO schema_version(version, applied_at) VALUES (25, '2026-01-01T00:00:00');
                INSERT INTO schema_version(version, applied_at) VALUES (34, '2026-01-01T00:00:00');
            """)
            conn.commit()
            conn.close()
            # 走正式迁移路径升级
            orig = config.DB_PATH
            config.DB_PATH = old
            db.DB_PATH = old
            try:
                db.init_db()
                lcols = {r["name"] for r in db.rows("PRAGMA table_info(concept_links)")}
                for c in ("strength", "reason", "evidence_ref"):
                    self.assertIn(c, lcols, f"v35 concept_links 应新增列 {c}")
                ccols = {r["name"] for r in db.rows("PRAGMA table_info(concepts)")}
                for c in ("evidence", "assessment_prompt"):
                    self.assertIn(c, ccols, f"v35 concepts 应新增列 {c}")
                # 存量边零损且落默认（soft/空理由/空锚点）
                legacy = db.row("SELECT * FROM concept_links WHERE concept_a = 1 AND concept_b = 2")
                self.assertEqual(legacy["relation"], "prerequisite", "存量边必须零损")
                self.assertEqual(legacy["strength"], "soft", "旧行 strength 落默认 soft")
                self.assertEqual(legacy["reason"], "")
                self.assertEqual(legacy["evidence_ref"], "")
                # 存量概念零损且判据/口试列落默认
                c1 = db.row("SELECT * FROM concepts WHERE id = 1")
                self.assertEqual(c1["name"], "牛顿第二定律")
                self.assertEqual(c1["evidence"], "")
                self.assertEqual(c1["assessment_prompt"], "")
                top = db.row("SELECT MAX(version) AS v FROM schema_version")["v"]
                self.assertGreaterEqual(int(top), 35)
                self.assertIn(35, [int(v["version"]) for v in db.rows("SELECT version FROM schema_version")])
                # 旧代码写入路径兼容：不带新列的 INSERT 仍可执行（回滚=撤代码不撤库）
                with db.DB_LOCK, db.db() as c2:
                    c2.execute(
                        "INSERT OR IGNORE INTO concept_links(concept_a, concept_b, relation) "
                        "VALUES (1, 2, 'related')")
                self.assertEqual(len(db.rows(
                    "SELECT 1 FROM concept_links WHERE concept_a = 1 AND concept_b = 2")), 2)
            finally:
                db.close_all_connections()  # 释放临时库句柄（Windows 文件锁）
                config.DB_PATH = orig
                db.DB_PATH = orig


class TestSettingsDict(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="settings_", dir=_TEST_TMP_DIR)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "settings_test.db"
        db.DB_PATH = config.DB_PATH  # db 模块按值绑定，需同步替换
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db
        os.environ.pop("LEARNOS_API_KEY", None)

    def test_secret_masked_by_default(self):
        # D1（R4）：DB 中写入 api_key 会被无视——密钥不落库。
        with db.db() as conn:
            conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('api_key', 'sk-secret123')")
        s = db.settings_dict()
        self.assertEqual(s["api_key"], "")
        self.assertFalse(s["has_api_key"])

    def test_secret_visible_with_flag(self):
        # D1（R4）：密钥绝不从 DB 读取，只来自 env/内存/keys.enc。
        with db.db() as conn:
            conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('api_key', 'sk-secret123')")
        s = db.settings_dict(include_secret=True)
        self.assertEqual(s["api_key"], "")
        self.assertEqual(s["key_source"], "none")

    def test_env_key_overrides(self):
        os.environ["LEARNOS_API_KEY"] = "sk-from-env"
        s = db.settings_dict(include_secret=True)
        self.assertEqual(s["api_key"], "sk-from-env")
        self.assertEqual(s["key_source"], "environment")
        os.environ.pop("LEARNOS_API_KEY", None)

    def test_no_key_returns_empty(self):
        os.environ.pop("LEARNOS_API_KEY", None)
        s = db.settings_dict()
        self.assertFalse(s["has_api_key"])
        self.assertEqual(s["api_key"], "")

    def test_db_api_key_row_cleared_by_migration(self):
        # v4 迁移应清除 DB 中的 api_key 残留行（模拟旧库：version 回退到 3）
        from db import _migrate
        with db.db() as conn:
            conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('api_key', 'sk-secret123')")
            conn.execute("DELETE FROM schema_version WHERE version >= 4")
        with db.db() as conn:
            _migrate(conn)
            rest = conn.execute("SELECT value FROM settings WHERE key = 'api_key'").fetchall()
            ver = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        self.assertEqual(rest, [])
        self.assertGreaterEqual(ver, 4)


if __name__ == "__main__":
    unittest.main()
