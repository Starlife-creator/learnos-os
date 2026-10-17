"""导出/提示领域层（exports.py、hints.py）单元测试 —— 不起 HTTP 服务。

#7 下沉的直接收益：CSV 转义、ICS 行尾与事件边界、提示缓存的档位与诊断门，
现在都能对真实 sqlite 断言。ADR 0010。
"""
import csv
import io
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
import exports
import hints

_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)


_NOW = "2026-10-16T09:00:00"


def _add_problem(subject: str = "physics", **fields) -> int:
    data = {"title": "题", "content": "题干", "course": "力学", "topic": "牛二",
            "created_at": _NOW, "updated_at": _NOW}
    data.update(fields)
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    with db.DB_LOCK, db.db() as conn:
        cur = conn.execute(
            f"INSERT INTO problems (subject, {cols}) VALUES (?, {marks})",
            (subject, *data.values()))
        return int(cur.lastrowid)


class DomainTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="exports_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "exports.db"
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
            for table in ("reviews", "hints", "problems"):
                conn.execute("DELETE FROM " + table)


class TestAnkiCsv(DomainTestBase):
    def test_quotes_commas_and_newlines_are_escaped(self):
        pid = _add_problem(title='含"引号"', content="a,b\nc")
        with db.DB_LOCK, db.db() as conn:
            conn.execute("INSERT INTO hints (problem_id, level, content, created_at) "
                         "VALUES (?, 3, ?, ?)",
                         (pid, "框架一", _NOW))
        body = exports.anki_csv("physics")
        self.assertTrue(body.startswith("\ufeff"), "Anki 需要 UTF-8 BOM")
        # 用标准 csv 解析回来：这正是「转义是否真的对」的判据，数行数会被字段内换行骗到
        records = list(csv.reader(io.StringIO(body.lstrip("\ufeff"))))
        self.assertEqual(len(records), 1, "字段内换行不能造出第二条记录")
        front, back, tags = records[0]
        self.assertEqual(front, '含"引号"\na,b\nc')
        self.assertIn("解题框架：框架一", back)
        self.assertIsInstance(tags, str)

    def test_no_notes_and_no_tags_yields_empty_tag_cell(self):
        """已知行为：tags 为空串时解析成空列表，会覆盖掉 course/topic 回退值。

        这是搬迁前的既有输出（纯搬迁不改可见行为），此处把现状钉住；真要修应作为
        独立改动（Anki 用户会看到标签丢失）。
        """
        _add_problem()
        body = exports.anki_csv("physics")
        self.assertIn("（无解析）", body)
        self.assertIn('",""', body, "标签列为空串")

    def test_explicit_tags_win_and_colons_are_replaced(self):
        _add_problem(tags='["几何:相似"]')
        body = exports.anki_csv("physics")
        self.assertIn("几何_相似", body)

    def test_only_current_subject_is_exported(self):
        _add_problem(subject="physics", title="物理题")
        _add_problem(subject="math", title="数学题")
        body = exports.anki_csv("physics")
        self.assertIn("物理题", body)
        self.assertNotIn("数学题", body)


class TestIcs(DomainTestBase):
    def _add_review(self, problem_id: int, due: str, completed: int = 0) -> None:
        with db.DB_LOCK, db.db() as conn:
            conn.execute(
                "INSERT INTO reviews (problem_id, due_date, interval_days, completed, created_at) "
                "VALUES (?, ?, 3, ?, ?)", (problem_id, due, completed, _NOW))

    def test_calendar_wraps_events_with_crlf(self):
        pid = _add_problem(title="斜面上的滑块")
        self._add_review(pid, "2026-10-20")
        body = exports.ics("physics")
        self.assertIn("\r\n", body)
        self.assertNotIn("\n\n", body)
        self.assertTrue(body.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(body.endswith("END:VCALENDAR"))
        self.assertIn("SUMMARY:复习：斜面上的滑块", body)
        self.assertIn("DTSTART:20261020T090000", body)
        self.assertEqual(body.count("BEGIN:VEVENT"), 1)

    def test_completed_reviews_are_not_exported(self):
        pid = _add_problem(title="已复习")
        self._add_review(pid, "2026-10-20", completed=1)
        self.assertEqual(exports.ics("physics").count("BEGIN:VEVENT"), 0)

    def test_empty_calendar_is_still_valid(self):
        body = exports.ics("physics")
        self.assertIn("BEGIN:VCALENDAR", body)
        self.assertIn("END:VCALENDAR", body)
        self.assertEqual(body.count("BEGIN:VEVENT"), 0)


class TestSnapshot(DomainTestBase):
    def test_shape_and_tags_are_parsed(self):
        pid = _add_problem(tags='["a", "b"]')
        with db.DB_LOCK, db.db() as conn:
            conn.execute("INSERT INTO hints (problem_id, level, content, created_at) "
                         "VALUES (?, 1, ?, ?)", (pid, "提示", _NOW))
            conn.execute(
                "INSERT INTO reviews (problem_id, due_date, interval_days, completed, created_at) "
                "VALUES (?, '2026-10-20', 3, 0, ?)", (pid, _NOW))
        data = exports.snapshot("physics")
        self.assertEqual(data["version"], 1)
        self.assertIn("exported_at", data)
        self.assertEqual(data["problems"][0]["tags"], ["a", "b"])
        self.assertEqual(len(data["hints"]), 1)
        self.assertEqual(len(data["reviews"]), 1)

    def test_broken_tags_json_degrades_to_empty_list(self):
        _add_problem(tags="{not json")
        data = exports.snapshot("physics")
        self.assertEqual(data["problems"][0]["tags"], [])


class TestHints(DomainTestBase):
    def _review(self, problem_id: int, result: str) -> None:
        with db.DB_LOCK, db.db() as conn:
            conn.execute(
                "INSERT INTO reviews (problem_id, due_date, interval_days, result, completed, created_at) "
                "VALUES (?, '2026-10-20', 3, ?, 1, ?)", (problem_id, result, _NOW))

    def test_diagnose_fires_only_on_failed_last_review(self):
        pid = _add_problem()
        self.assertFalse(hints.diagnose_needed(pid), "没有复习记录时不附诊断")
        self._review(pid, "4")
        self.assertFalse(hints.diagnose_needed(pid))
        self._review(pid, "2")
        self.assertTrue(hints.diagnose_needed(pid), "最近一次是「模糊」应附诊断")
        self._review(pid, "4")
        self.assertFalse(hints.diagnose_needed(pid), "只看最近一次")

    def test_diagnose_tolerates_non_numeric_result(self):
        pid = _add_problem()
        self._review(pid, "")
        self.assertFalse(hints.diagnose_needed(pid))

    def test_cache_is_per_level_and_latest_saved_wins(self):
        pid = _add_problem()
        self.assertIsNone(hints.cached(pid, 1))
        hints.save(pid, 1, "一级提示")
        hints.save(pid, 3, "三级提示")
        self.assertEqual(hints.cached(pid, 1), "一级提示")
        self.assertEqual(hints.cached(pid, 3), "三级提示")
        self.assertIsNone(hints.cached(pid, 2))
        hints.save(pid, 1, "一级提示v2")
        self.assertIn(hints.cached(pid, 1), ("一级提示", "一级提示v2"))


if __name__ == "__main__":
    unittest.main()
