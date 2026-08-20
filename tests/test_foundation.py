"""阶段0 地基测试：PRAGMA / 配置单一真相源 / 导出令牌 / _get_or_404 收口。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

# 在导入项目模块前把数据库指向临时文件，避免污染真实库
_TMP = tempfile.mkdtemp(prefix="learnos_foundation_")
os.environ["LEARNOS_DB"] = str(Path(_TMP) / "test.db")

import config  # noqa: E402
import db  # noqa: E402
import auth  # noqa: E402

config.DB_PATH = Path(_TMP) / "test.db"
db.DB_PATH = config.DB_PATH
db.init_db()

# 导入 handler 相关件（在 DB 路径固定后）
import handler as handler_mod  # noqa: E402
from handler_problems import ProblemsMixin  # noqa: E402
from handler_reports import ReportsMixin  # noqa: E402
import material  # noqa: E402


class TestConfigSchema(unittest.TestCase):
    """§16.3 配置单一真相源：schema 驱动默认值与 coercion。"""

    def test_defaults_derived_from_schema(self):
        self.assertEqual(config.DEFAULT_SETTINGS["temperature"], "0.3")
        self.assertEqual(config.DEFAULT_SETTINGS["default_subject"], "physics")
        self.assertIn("allow_local_ai", config.DEFAULT_SETTINGS)

    def test_coerce_float_clamps_and_falls_back(self):
        self.assertEqual(config.coerce_setting("temperature", ""), "0.3")
        self.assertEqual(config.coerce_setting("temperature", "3.5"), "2.0")  # 上限
        self.assertEqual(config.coerce_setting("temperature", "0.2"), "0.2")
        self.assertEqual(config.coerce_setting("temperature", "abc"), "0.3")  # 非法回退

    def test_coerce_int_clamps(self):
        self.assertEqual(config.coerce_setting("daily_review_cap", "999"), "500")
        self.assertEqual(config.coerce_setting("daily_review_cap", "-5"), "0")
        self.assertEqual(config.coerce_setting("daily_review_cap", ""), "0")

    def test_coerce_bool(self):
        self.assertEqual(config.coerce_setting("allow_local_ai", "off"), "0")
        self.assertEqual(config.coerce_setting("allow_local_ai", "1"), "1")
        self.assertEqual(config.coerce_setting("hint_cache_enabled", "false"), "0")

    def test_coerce_subject_uses_validator(self):
        # 内置三科合法；未知学科回退默认 physics（valid_subject_fn 由 handler 注入）
        self.assertEqual(config.coerce_setting("default_subject", "math",
                                               valid_subject_fn=lambda s: s if s == "math" else "physics"), "math")
        self.assertEqual(config.coerce_setting("default_subject", "bogus",
                                               valid_subject_fn=lambda s: s if s == "math" else "physics"), "physics")

    def test_coerce_rejects_unknown_key(self):
        with self.assertRaises(ValueError):
            config.coerce_setting("not_a_key", "x")


class TestProdPragma(unittest.TestCase):
    """§1.3/§16.1 生产级 PRAGMA 生效。"""

    def setUp(self):
        self.conn = db.connect()
        self.cur = self.conn.cursor()

    def tearDown(self):
        self.conn.close()

    def test_pragmas(self):
        self.assertEqual(self.cur.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.cur.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.cur.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        self.assertEqual(self.cur.execute("PRAGMA synchronous").fetchone()[0], 1)  # NORMAL
        self.assertEqual(self.cur.execute("PRAGMA temp_store").fetchone()[0], 2)  # MEMORY
        self.assertLessEqual(self.cur.execute("PRAGMA cache_size").fetchone()[0], -1000)


class _Stub(ProblemsMixin):
    """最小桩：仅提供 export token 测试所需的 path/headers/json_response。"""
    def __init__(self, path: str, token_header: str | None = None):
        self.path = path
        self._hdr = {}
        if token_header:
            self._hdr["X-Export-Token"] = token_header
        self.captured = None

    @property
    def headers(self):
        class _H:
            def get(_self, k, d=None):
                return self._hdr.get(k, d)
        return _H()

    def json_response(self, data, status=200):
        self.captured = (status, data)


class TestExportToken(unittest.TestCase):
    """§1.1/§16.6 导出端点强制一次性令牌。"""

    def test_missing_token_rejected(self):
        s = _Stub("/api/export?format=json")
        ok = s._export_token_ok()
        self.assertFalse(ok)

    def test_wrong_token_rejected(self):
        s = _Stub(f"/api/export?token=wrong", token_header="wrong")
        # query 优先于 header；wrong != EXPORT_TOKEN
        s.path = "/api/export?token=wrong"
        self.assertFalse(s._export_token_ok())

    def test_correct_query_token_accepted(self):
        tok = auth.issue_export_challenge(ip="127.0.0.1")[0]  # R5：签名绑定 IP，须与 _Stub 默认 _client_ip 一致
        s = _Stub(f"/api/export?token={tok}")
        self.assertTrue(s._export_token_ok())

    def test_correct_header_token_accepted(self):
        tok = auth.issue_export_challenge(ip="127.0.0.1")[0]
        s = _Stub("/api/export", token_header=tok)
        self.assertTrue(s._export_token_ok())


class _StubHandler(handler_mod.Handler):
    """绕过 http 基类初始化的桩，仅用于 _get_or_404。"""
    def __init__(self):
        self.captured = None
        self.path = "/x"
        self.headers = {}

    def json_response(self, data, status=200):
        self.captured = (status, data)


class TestGetOr404(unittest.TestCase):
    """§16.5 _get_or_404 收口，含 SQL 注入白名单。"""

    def test_found(self):
        with db.DB_LOCK, db.db() as conn:
            cur = conn.execute(
                "INSERT INTO problems(title, course, topic, content, created_at, updated_at) "
                "VALUES ('t','c','tp','ct',?,?)",
                ("2026-01-01T00:00:00", "2026-01-01T00:00:00"),
            )
            pid = cur.lastrowid
        h = _StubHandler()
        row = h._get_or_404("problems", pid, "题目不存在")
        self.assertIsNotNone(row)
        self.assertEqual(row["title"], "t")

    def test_not_found_returns_404(self):
        h = _StubHandler()
        res = h._get_or_404("problems", 999999, "题目不存在")
        self.assertIsNone(res)
        self.assertEqual(h.captured[0], 404)

    def test_sql_injection_blocked(self):
        h = _StubHandler()
        res = h._get_or_404("problems; DROP TABLE problems", 1, "x")
        self.assertIsNone(res)
        self.assertEqual(h.captured[0], 400)


class _StubReports(ReportsMixin):
    def __init__(self, subject: str):
        self.subject = subject
        self.captured = None

    def json_response(self, data, status=200):
        self.captured = (status, data)


class TestProgressDashboard(unittest.TestCase):
    """§42.1/§42.2 可见进步仪表盘 + 微学习节奏。"""

    def setUp(self):
        # 隔离：清空同享临时库的遗留数据
        with db.DB_LOCK, db.db() as conn:
            conn.execute("DELETE FROM problems")
            conn.execute("DELETE FROM reviews")

    def _insert(self, title, mastery, created):
        with db.DB_LOCK, db.db() as conn:
            conn.execute(
                "INSERT INTO problems(title, course, topic, content, mastery, created_at, updated_at) "
                "VALUES (?, '', '', '', ?, ?, ?)",
                (title, mastery, created, created),
            )

    def test_progress_empty(self):
        h = _StubReports("physics")
        h._handle_progress()
        data = h.captured[1]
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["mastery_pct"], 0.0)
        self.assertEqual(data["streak_days"], 0)
        self.assertTrue(data["micro_unit"]["steps"])

    def test_progress_with_data_and_streak(self):
        today = __import__("datetime").date.today().isoformat()
        for i in range(3):
            self._insert(f"p{i}", 5, today)
        h = _StubReports("physics")
        h._handle_progress()
        data = h.captured[1]
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["mastery_pct"], 100.0)
        self.assertEqual(data["mastered"], 3)
        self.assertEqual(data["streak_days"], 1)  # 今天有活动
        self.assertLessEqual(data["micro_unit"]["est_minutes"], 10)


class TestReadingLoop(unittest.TestCase):
    """§27 读书闭环基础版：文本 → 原子卡 → 落库进 FSRS。"""

    def setUp(self):
        with db.DB_LOCK, db.db() as conn:
            conn.execute("DELETE FROM problems")

    def test_heuristic_atomic_cards_from_markdown(self):
        text = "# 牛顿第一定律\n物体不受外力时保持静止或匀速直线运动。\n\n# 惯性\n物体保持运动状态的属性叫惯性。"
        cards = material.extract_atomic_cards(text, "physics", use_ai=False)
        self.assertGreaterEqual(len(cards), 2)
        self.assertTrue(any("牛顿第一定律" in c["concept"] for c in cards))
        for c in cards:
            self.assertIn("question", c)
            self.assertIn("answer", c)
            self.assertTrue(c["answer"])

    def test_apply_cards_persists(self):
        text = "# 概念A\n这是概念A的解释内容。\n\n# 概念B\n这是概念B的解释内容。"
        cards = material.extract_atomic_cards(text, "math", use_ai=False)
        added = material.apply_cards(cards, "math")
        self.assertEqual(added, len(cards))
        persisted = db.rows("SELECT id, subject, title FROM problems WHERE subject = 'math'")
        self.assertEqual(len(persisted), added)
        self.assertTrue(all("概念" in p["title"] for p in persisted))

    def test_empty_text_no_cards(self):
        self.assertEqual(material.extract_atomic_cards("", "physics", use_ai=False), [])


if __name__ == "__main__":
    unittest.main()
