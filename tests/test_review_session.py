"""复习会话领域层（review_session.py）单元测试 —— 不起 HTTP 服务。

这是 #7「业务逻辑下沉」的收益验证：队列优先级、每日上限、交错、完成复习的事务
写回、今日小结，现在都能直接对真实 sqlite 断言，不必经由端点。ADR 0010。
"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai
import config
import db
import review_session

_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)
TODAY = date.today()


def _date(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


def _ts(days: int) -> str:
    return _date(days) + "T09:00:00"


class ReviewSessionTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="review_session_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "review_session.db"
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
            for table in ("mastery_events", "oral_sessions", "reviews", "problems",
                          "mastery_log"):
                conn.execute("DELETE FROM " + table)
            conn.execute("DELETE FROM settings WHERE key = 'daily_review_cap'")
        ai.invalidate_settings_cache()

    # -- 构造 ------------------------------------------------------------
    def _problem(self, title: str, *, topic: str = "力学", mastery: int = 3,
                 error_type: str = "概念不清", repetition: int = 2,
                 stability: float = 5.0, state: int = 2) -> int:
        with db.DB_LOCK, db.db() as conn:
            cur = conn.execute(
                "INSERT INTO problems (title, content, subject, topic, course, error_type, "
                "mastery, repetition, state, stability, created_at, updated_at) "
                "VALUES (?, ?, 'physics', ?, '第一章', ?, ?, ?, ?, ?, ?, ?)",
                (title, f"{title} 题干", topic, error_type, mastery, repetition, state,
                 stability, _ts(-30), _ts(0)),
            )
            return int(cur.lastrowid)

    def _review(self, pid: int, *, due_date: str = "", interval: int = 3,
                completed: int = 0, result: str = "", created_at: str = "",
                variant_id: int = 0) -> int:
        with db.DB_LOCK, db.db() as conn:
            cur = conn.execute(
                "INSERT INTO reviews (problem_id, due_date, interval_days, completed, result, "
                "created_at, variant_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (pid, due_date or _date(0), interval, completed, result,
                 created_at or _ts(-3), variant_id),
            )
            return int(cur.lastrowid)


class TestInterleave(ReviewSessionTestBase):
    def test_separates_same_topic(self):
        items = [{"topic": "A"}, {"topic": "A"}, {"topic": "B"}]
        out = review_session.interleave(items)
        self.assertEqual(len(out), 3)
        self.assertNotEqual(out[0]["topic"], out[1]["topic"])

    def test_single_bucket_keeps_order(self):
        items = [{"topic": "A", "id": 1}, {"topic": "A", "id": 2}]
        self.assertEqual([i["id"] for i in review_session.interleave(items)], [1, 2])

    def test_missing_topic_treated_as_uncategorized(self):
        items = [{"id": 1}, {"id": 2, "topic": ""}]
        out = review_session.interleave(items)
        self.assertEqual(sorted(i["id"] for i in out), [1, 2])


class TestQueue(ReviewSessionTestBase):
    def test_overdue_first_then_error_weight(self):
        """逾期久 > 错因权重：这是队列的核心承诺，退化了就等于队列失效。"""
        p_fresh = self._problem("今天到期-概念不清", error_type="概念不清")     # 权重 3
        p_overdue = self._problem("逾期十天-执行失误", error_type="执行失误")   # 权重 0
        p_today_trap = self._problem("今天到期-审题陷阱", error_type="审题陷阱")  # 权重 2
        self._review(p_fresh, due_date=_date(0))
        self._review(p_overdue, due_date=_date(-10))
        self._review(p_today_trap, due_date=_date(0))
        payload = review_session.queue("physics", plain=True)
        ids = [i["problem_id"] for i in payload["items"]]
        self.assertEqual(ids, [p_overdue, p_fresh, p_today_trap])
        # 逾期压过一切（哪怕它的错因权重最低 0）
        self.assertEqual(payload["total"], 3)
        self.assertFalse(payload["capped"])

    def test_stability_breaks_tie(self):
        """同逾期、同错因时，FSRS 稳定性低（记忆更脆弱）优先重考。"""
        p_fragile = self._problem("脆弱", stability=1.0)
        p_stable = self._problem("稳固", stability=80.0)
        self._review(p_fragile, due_date=_date(0))
        self._review(p_stable, due_date=_date(0))
        ids = [i["problem_id"] for i in review_session.queue("physics", plain=True)["items"]]
        self.assertEqual(ids, [p_fragile, p_stable])

    def test_feynman_gaps_badge_and_priority(self):
        p_gap = self._problem("有漏点")
        p_clean = self._problem("无漏点")
        self._review(p_gap, due_date=_date(0))
        self._review(p_clean, due_date=_date(0))
        with db.DB_LOCK, db.db() as conn:
            conn.execute(
                "INSERT INTO oral_sessions (problem_id, mode, topic, self_review, created_at) "
                "VALUES (?, 'feynman', '边界条件', ?, ?)",
                (p_gap, '{"gaps": ["没讲清边界", "漏了单位"]}', _ts(0)),
            )
        items = {i["problem_id"]: i for i in review_session.queue("physics", plain=True)["items"]}
        self.assertEqual(items[p_gap]["feynman_gaps"], 2)
        self.assertEqual(items[p_clean]["feynman_gaps"], 0)

    def test_daily_cap(self):
        for i in range(3):
            pid = self._problem(f"题{i}")
            self._review(pid, due_date=_date(0))
        with db.DB_LOCK, db.db() as conn:
            conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('daily_review_cap', '2')")
        ai.invalidate_settings_cache()
        payload = review_session.queue("physics")
        self.assertEqual(payload["cap"], 2)
        self.assertEqual(payload["total"], 3)         # total 是上限前的真实到期数
        self.assertEqual(len(payload["items"]), 2)
        self.assertTrue(payload["capped"])

    def test_plain_mode_skips_interleaving(self):
        """plain=True 只按优先级排序；默认会把同知识点的相邻项隔开。"""
        p1 = self._problem("A1", topic="A")
        p2 = self._problem("A2", topic="A")
        p3 = self._problem("B1", topic="B")
        self._review(p1, due_date=_date(-3))
        self._review(p2, due_date=_date(-2))
        self._review(p3, due_date=_date(-1))
        plain = [i["problem_id"] for i in review_session.queue("physics", plain=True)["items"]]
        mixed = [i["problem_id"] for i in review_session.queue("physics")["items"]]
        self.assertEqual(plain, [p1, p2, p3])
        self.assertEqual(mixed, [p1, p3, p2])

    def test_only_incomplete_and_own_subject(self):
        p_mine = self._problem("我的题")
        done = self._review(p_mine, completed=1, result="4", created_at=_ts(-1))
        self._review(p_mine, due_date=_date(0))
        with db.DB_LOCK, db.db() as conn:
            conn.execute(
                "INSERT INTO problems (title, content, subject, created_at, updated_at) "
                "VALUES ('别科题', 'x', 'chemistry', ?, ?)", (_ts(0), _ts(0)))
            other = int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
        self._review(other, due_date=_date(0))
        ids = [i["problem_id"] for i in review_session.queue("physics")["items"]]
        self.assertEqual(ids, [p_mine])
        with db.DB_LOCK, db.db() as conn:
            got = conn.execute("SELECT completed FROM reviews WHERE id = ?", (done,)).fetchone()
        self.assertEqual(got["completed"], 1)  # 已完成行不再进队列


class TestComplete(ReviewSessionTestBase):
    def test_writes_next_review_and_mastery_event(self):
        pid = self._problem("复习题", mastery=3)
        rid = self._review(pid, due_date=_date(0), interval=3)
        payload = review_session.complete(rid, 4, "physics")
        self.assertGreaterEqual(payload["interval_days"], 1)
        self.assertTrue(payload["next_due"])
        with db.DB_LOCK, db.db() as conn:
            old = conn.execute("SELECT completed, result FROM reviews WHERE id = ?", (rid,)).fetchone()
            self.assertEqual(old["completed"], 1)
            self.assertEqual(old["result"], "4")
            nxt = conn.execute(
                "SELECT due_date, interval_days FROM reviews WHERE problem_id = ? AND completed = 0",
                (pid,)).fetchall()
            self.assertEqual(len(nxt), 1)                       # 恰有一条新的待复习行
            self.assertEqual(nxt[0]["due_date"], payload["next_due"])
            self.assertEqual(nxt[0]["interval_days"], payload["interval_days"])
            ev = conn.execute(
                "SELECT entry_point, item_type, item_id, cur_mastery FROM mastery_events "
                "WHERE item_type = 'problem' AND item_id = ? ORDER BY id DESC LIMIT 1",
                (pid,)).fetchone()
        self.assertIsNotNone(ev, "复习完成必须经 set_problem_mastery 落事件（ADR 0004）")
        self.assertEqual(ev["entry_point"], "review")

    def test_missing_review_is_404(self):
        with self.assertRaises(review_session.ReviewError) as ctx:
            review_session.complete(999999, 3, "physics")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.message, "复习任务不存在")

    def test_rating_is_clamped_to_1_4(self):
        pid = self._problem("评分钳位")
        rid_hi = self._review(pid, due_date=_date(0))
        review_session.complete(rid_hi, 99, "physics")
        rid_lo = self._review(pid, due_date=_date(0))
        review_session.complete(rid_lo, -5, "physics")
        with db.DB_LOCK, db.db() as conn:
            hi = conn.execute("SELECT result FROM reviews WHERE id = ?", (rid_hi,)).fetchone()
            lo = conn.execute("SELECT result FROM reviews WHERE id = ?", (rid_lo,)).fetchone()
        self.assertEqual((hi["result"], lo["result"]), ("4", "1"))

    def test_mastery_log_snapshot_written(self):
        """完成后写当日掌握度快照（趋势图数据源，与事件表语义不同，ADR 0004）。"""
        pid = self._problem("快照题", mastery=5)
        rid = self._review(pid, due_date=_date(0))
        review_session.complete(rid, 3, "physics")
        with db.DB_LOCK, db.db() as conn:
            snap = conn.execute(
                "SELECT avg_mastery, count FROM mastery_log WHERE day = ? AND subject = 'physics'",
                (_date(0),)).fetchone()
        self.assertIsNotNone(snap)
        self.assertGreater(snap["count"], 0)


class TestReschedule(ReviewSessionTestBase):
    def test_moves_due_date_to_today(self):
        pid = self._problem("顺延题")
        rid = self._review(pid, due_date=_date(7))
        review_session.reschedule(rid)
        with db.DB_LOCK, db.db() as conn:
            got = conn.execute("SELECT due_date FROM reviews WHERE id = ?", (rid,)).fetchone()
        self.assertEqual(got["due_date"], _date(0))

    def test_missing_review_is_404(self):
        with self.assertRaises(review_session.ReviewError) as ctx:
            review_session.reschedule(999999)
        self.assertEqual(ctx.exception.status, 404)


class TestTodaySummary(ReviewSessionTestBase):
    def test_counts_today_results_and_error_distribution(self):
        p1 = self._problem("题一", error_type="概念不清")
        p2 = self._problem("题二", error_type="概念不清")
        p3 = self._problem("题三", error_type="计算失误")
        self._review(p1, completed=1, result="4", created_at=_ts(0))
        self._review(p2, completed=1, result="1", created_at=_ts(0))
        self._review(p3, completed=1, result="2", created_at=_ts(0))
        # 昨天完成的不算今天
        self._review(p1, completed=1, result="4", created_at=_ts(-1))
        out = review_session.today_summary("physics")
        self.assertEqual(out["date"], _date(0))
        self.assertEqual(out["done"], 3)
        self.assertEqual(out["hard"], 2)              # result <= 2
        self.assertEqual(out["accuracy"], 33)         # round((1-2/3)*100)
        self.assertEqual(out["top_error"], "概念不清")
        self.assertEqual(out["warmup"], [3])

    def test_empty_day_is_zero_not_crash(self):
        out = review_session.today_summary("physics")
        self.assertEqual(out["done"], 0)
        self.assertEqual(out["accuracy"], 0)
        self.assertEqual(out["warmup"], [])
        self.assertEqual(out["top_error"], "")


if __name__ == "__main__":
    unittest.main()
