"""学科领域层（subjects.py）单元测试 —— 不起 HTTP 服务。

#7 的收益验证：学科总览/新增/级联删除/全局搜索现在能直接断言，不必构造会话；
删除的 scope 边界（哪张表被清、内置学科为何拒删）此前只能靠人工试。
"""
import tempfile
import unittest
from datetime import date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
import subjects

_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)
NOW = date.today().isoformat() + "T09:00:00"


class SubjectsTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="subjects_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "subjects.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        db.close_all_connections()
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db
        cls._tmp.cleanup()

    def setUp(self):
        with db.DB_LOCK, db.db() as conn:
            for table in ("concept_links", "concepts", "problems", "bank_problems",
                          "bank_scores", "study_checkins", "mastery_log",
                          "seed_versions"):
                conn.execute("DELETE FROM " + table)
            conn.execute("DELETE FROM subjects WHERE builtin = 0")

    def _concept(self, subject: str, name: str = "概念") -> int:
        with db.DB_LOCK, db.db() as conn:
            cur = conn.execute(
                "INSERT INTO concepts (subject, name, created_at) VALUES (?, ?, ?)",
                (subject, name, NOW))
            return int(cur.lastrowid)

    def _builtin_subject(self) -> str:
        with db.DB_LOCK, db.db() as conn:
            r = conn.execute("SELECT id FROM subjects WHERE builtin = 1 LIMIT 1").fetchone()
        return r["id"]


class TestAdd(SubjectsTestBase):
    def test_creates_subject_and_normalizes_id(self):
        got = subjects.add("Organic_Chem", "有机化学")
        self.assertEqual(got, {"id": "organic_chem", "title": "有机化学", "builtin": False})
        with db.DB_LOCK, db.db() as conn:
            r = conn.execute("SELECT title, builtin FROM subjects WHERE id = 'organic_chem'").fetchone()
        self.assertEqual((r["title"], r["builtin"]), ("有机化学", 0))

    def test_title_defaults_to_id(self):
        self.assertEqual(subjects.add("optics")["title"], "optics")

    def test_rejects_bad_id(self):
        for bad in ("9lives", "有中文", "x" * 21, ""):
            with self.assertRaises(subjects.SubjectsError) as ctx:
                subjects.add(bad)
            self.assertEqual(ctx.exception.status, 400)
            self.assertIn("学科 id 需字母开头", ctx.exception.message)

    def test_rejects_duplicate(self):
        subjects.add("dup")
        with self.assertRaises(subjects.SubjectsError) as ctx:
            subjects.add("dup")
        self.assertEqual(ctx.exception.status, 409)


class TestDelete(SubjectsTestBase):
    def test_unknown_subject_is_404(self):
        with self.assertRaises(subjects.SubjectsError) as ctx:
            subjects.delete("nope")
        self.assertEqual((ctx.exception.status, ctx.exception.message), (404, "学科不存在"))

    def test_builtin_cannot_be_deleted(self):
        sid = self._builtin_subject()
        with self.assertRaises(subjects.SubjectsError) as ctx:
            subjects.delete(sid)
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("内置学科不可删除", ctx.exception.message)

    def test_unknown_scope_is_400(self):
        subjects.add("scoped")
        with self.assertRaises(subjects.SubjectsError) as ctx:
            subjects.delete("scoped", "everything")
        self.assertEqual(ctx.exception.status, 400)

    def test_graph_scope_keeps_problems_and_subject_row(self):
        sid = subjects.add("scope_test")["id"]
        cid = self._concept(sid)
        with db.DB_LOCK, db.db() as conn:
            conn.execute(
                "INSERT INTO concept_links (concept_a, concept_b, relation) VALUES (?, ?, 'prerequisite')",
                (cid, cid))
            conn.execute(
                "INSERT INTO problems (title, content, subject, created_at, updated_at) "
                "VALUES ('题', 'x', ?, ?, ?)", (sid, NOW, NOW))
        got = subjects.delete(sid, "graph")
        self.assertEqual(got["scope"], "graph")
        self.assertEqual(got["deleted"]["concepts"], 1)
        self.assertEqual(got["deleted"]["concept_links"], 1)
        with db.DB_LOCK, db.db() as conn:
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM subjects WHERE id = ?", (sid,)).fetchone(), "graph scope 不该删学科注册")
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM problems WHERE subject = ?", (sid,)).fetchone(), "graph scope 不该动个人数据")

    def test_personal_scope_clears_problems_only(self):
        sid = subjects.add("personal_test")["id"]
        self._concept(sid)
        with db.DB_LOCK, db.db() as conn:
            conn.execute(
                "INSERT INTO problems (title, content, subject, created_at, updated_at) "
                "VALUES ('题', 'x', ?, ?, ?)", (sid, NOW, NOW))
            conn.execute(
                "INSERT INTO study_checkins (subject, check_date, minutes, note, created_at) "
                "VALUES (?, ?, 25, '', ?)", (sid, NOW[:10], NOW))
        got = subjects.delete(sid, "personal")
        self.assertEqual(got["deleted"]["problems"], 1)
        self.assertEqual(got["deleted"]["study_checkins"], 1)
        with db.DB_LOCK, db.db() as conn:
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM concepts WHERE subject = ?", (sid,)).fetchone(), "personal scope 不该动图谱")

    def test_full_scope_removes_subject_row(self):
        sid = subjects.add("full_test")["id"]
        self._concept(sid)
        got = subjects.delete(sid)
        self.assertEqual(got["scope"], "full")
        self.assertIn("subjects", got["deleted"])
        with db.DB_LOCK, db.db() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM subjects WHERE id = ?", (sid,)).fetchone())
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM concepts WHERE subject = ?", (sid,)).fetchone())


class TestOverview(SubjectsTestBase):
    def test_includes_counts_and_seed_flags(self):
        sid = subjects.add("overview_test", "概览")["id"]
        self._concept(sid, "牛顿第一定律")
        with db.DB_LOCK, db.db() as conn:
            conn.execute(
                "INSERT INTO problems (title, content, subject, created_at, updated_at) "
                "VALUES ('题', 'x', ?, ?, ?)", (sid, NOW, NOW))
        rows = {s["id"]: s for s in subjects.overview()}
        self.assertIn(sid, rows)
        item = rows[sid]
        self.assertEqual(item["title"], "概览")
        self.assertEqual(item["concept_count"], 1)
        # 题库是 JSON 文件：没有题库文件时题数回 0，而不是抛异常
        self.assertEqual(item["question_count"], 0)
        self.assertIn("has_seed_graph", item)
        self.assertIn("seed_needs_update", item)

    def test_builtin_subjects_are_listed(self):
        ids = [s["id"] for s in subjects.overview()]
        self.assertIn(self._builtin_subject(), ids)


class TestSearch(SubjectsTestBase):
    def _problem(self, title: str, subject: str = "physics") -> int:
        with db.DB_LOCK, db.db() as conn:
            cur = conn.execute(
                "INSERT INTO problems (title, content, topic, subject, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", (title, "题干内容", "力学", subject, NOW, NOW))
            return int(cur.lastrowid)

    def test_empty_query_returns_empty_groups(self):
        self.assertEqual(subjects.search("physics", "   "),
                         {"problems": [], "concepts": [], "bank": [], "docs": []})

    def test_matches_problem_title(self):
        pid = self._problem("斜面摩擦力计算")
        self._problem("无关题目")
        out = subjects.search("physics", "摩擦力")
        self.assertEqual([p["id"] for p in out["problems"]], [pid])
        self.assertEqual(set(out), {"problems", "concepts", "bank", "docs"})

    def test_respects_subject_scope(self):
        self._problem("跨科同名题", subject="chemistry")
        self.assertEqual(subjects.search("physics", "跨科同名题")["problems"], [])

    def test_limit_is_applied(self):
        for i in range(4):
            self._problem(f"同名题{i}")
        self.assertEqual(len(subjects.search("physics", "同名题", limit=2)["problems"]), 2)


if __name__ == "__main__":
    unittest.main()
