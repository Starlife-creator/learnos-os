"""ADR 0013 口径守卫：闪卡（cards）必须与题目一起进入学习活动/健康度聚合。

#11 之前 reports.py 的 25 处查询只走 problems/reviews —— 用户复习了 20 张闪卡，
今日任务、压力指数、周报、北极星里一张都不体现。统一读模型（db.py v37 的
review_items / review_log 只读视图）修的就是这个口径。

本文件用一份最小数据集把每个消费者钉住：任何一处退回 problems-only 就会失败。
刻意不测写路径 —— 视图是只读的，写路径仍是两套（reviews / card_reviews），
「为什么不合表」见 ADR 0013。
"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_rules
import config
import db
import profile
import reports

_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)
TODAY = date.today()

# 数据集规模（改数据集时同步改这里，让口径断言保持可读）
ITEMS = 5       # 3 道题 + 2 张 active 闪卡（停用卡不算可复习项）
LOGS = 4        # 1 条题目复习 + 3 条卡片复习（undone=1 的作废行不算）


def _date(days: int) -> str:
    """相对今天的天数（负数=过去）。"""
    return (TODAY + timedelta(days=days)).isoformat()


def _ts(days: int) -> str:
    return _date(days) + "T09:00:00"


class ReviewModelTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="review_model_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "review_model.db"
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
            for table in ("card_reviews", "cards", "reviews", "problems"):
                conn.execute("DELETE FROM " + table)
            conn.execute("DELETE FROM settings WHERE key LIKE 'fsrs_desired_retention%'")
        # 题目：到期题（有到期复习行 + 一条 40 天前的完成记录）
        #       未来题（待复习行在 5 天后）
        #       从未排过复习行的题（结构性指标的分母，due_date 必须为 NULL 而不是丢掉）
        self.p_due = self._problem("到期题", topic="力学", mastery=3, repetition=4,
                                   state=2, stability=10.0)
        self.p_future = self._problem("未来题", topic="力学", mastery=4, repetition=1,
                                      state=2, stability=20.0)
        self.p_none = self._problem("从未排过的题", mastery=1, repetition=0)
        self._review(self.p_due, due_date=_date(0), interval=7)
        self._review(self.p_future, due_date=_date(5), interval=3)
        self._review(self.p_due, due_date=_date(-40), interval=4,
                     completed=1, result="3", created_at=_ts(-40))
        # 闪卡：到期卡（20 天前 + 今天各复习一次）/ 未来卡 / 停用卡（不得出现）
        self.c_due = self._card("到期卡", status="active", due_date=_date(0), interval=5,
                                repetition=2, state=2, stability=12.0)
        self.c_future = self._card("未来卡", status="active", due_date=_date(10), interval=10,
                                   repetition=1, state=2, stability=15.0)
        self.c_off = self._card("停用卡", status="disabled", due_date=_date(0))
        self._card_review(self.c_due, rating=3, created_at=_ts(-20), due_date=_date(-20))
        self._card_review(self.c_due, rating=4, created_at=_ts(0), due_date=_date(0))
        # 作废行（undone=1）：既不算复习次数，也不算北极星样本
        self._card_review(self.c_due, rating=1, created_at=_ts(0), due_date=_date(0), undone=1)
        self._card_review(self.c_future, rating=3, created_at=_ts(-40), due_date=_date(-40),
                          interval=10)

    # -- 构造 ------------------------------------------------------------
    def _problem(self, title: str, topic: str = "", mastery: int = 1, repetition: int = 0,
                 state: int = 0, stability: float = 0.0) -> int:
        with db.DB_LOCK, db.db() as conn:
            cur = conn.execute(
                "INSERT INTO problems (title, content, subject, topic, course, error_type, "
                "mastery, repetition, state, stability, created_at, updated_at) "
                "VALUES (?, ?, 'physics', ?, '第一章', '概念不清', ?, ?, ?, ?, ?, ?)",
                (title, f"{title} 题干", topic, mastery, repetition, state, stability,
                 _ts(-40), _ts(0)),
            )
            return int(cur.lastrowid)

    def _review(self, pid: int, due_date: str, interval: int, completed: int = 0,
                result: str = "", created_at: str = "") -> int:
        with db.DB_LOCK, db.db() as conn:
            cur = conn.execute(
                "INSERT INTO reviews (problem_id, due_date, interval_days, completed, result, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (pid, due_date, interval, completed, result, created_at or _ts(-40)),
            )
            return int(cur.lastrowid)

    def _card(self, cue: str, status: str = "active", due_date: str = "", interval: int = 1,
              repetition: int = 0, state: int = 0, stability: float = 0.0) -> int:
        with db.DB_LOCK, db.db() as conn:
            cur = conn.execute(
                "INSERT INTO cards (subject, kind, cue, answer, status, source, created_at, "
                "repetition, state, stability, due_date, interval_days) "
                "VALUES ('physics', 'qa', ?, ?, ?, 'manual', ?, ?, ?, ?, ?, ?)",
                (cue, f"{cue} 答案", status, _ts(-40), repetition, state, stability,
                 due_date, interval),
            )
            return int(cur.lastrowid)

    def _card_review(self, cid: int, rating: int, created_at: str, due_date: str = "",
                     interval: int = 1, undone: int = 0) -> int:
        with db.DB_LOCK, db.db() as conn:
            cur = conn.execute(
                "INSERT INTO card_reviews (card_id, due_date, interval_days, rating, created_at, "
                "undone) VALUES (?, ?, ?, ?, ?, ?)",
                (cid, due_date, interval, rating, created_at, undone),
            )
            return int(cur.lastrowid)


class TestReviewItemsView(ReviewModelTestBase):
    """读模型本身：一行一个可复习项，题目全部保留，只有排过期的 active 卡才算。"""

    def test_rows_are_problems_plus_active_cards(self):
        got = db.rows(
            "SELECT item_type, COUNT(*) AS c FROM review_items "
            "WHERE subject = 'physics' GROUP BY item_type")
        self.assertEqual({r["item_type"]: r["c"] for r in got}, {"problem": 3, "card": 2})
        self.assertEqual(sum(r["c"] for r in got), ITEMS)

    def test_problem_without_pending_review_is_kept_with_null_due(self):
        """没有待复习行的题目必须仍在（成熟度分母），due_date 为 NULL 而不是被 JOIN 丢掉。"""
        got = db.rows("SELECT due_date FROM review_items "
                      "WHERE item_type = 'problem' AND item_id = ?", (self.p_none,))
        self.assertEqual(len(got), 1)
        self.assertIsNone(got[0]["due_date"])

    def test_disabled_card_and_unscheduled_card_are_not_reviewable(self):
        labels = [r["label"] for r in db.rows("SELECT label FROM review_items")]
        self.assertNotIn("停用卡", labels)

    def test_review_log_normalizes_rating_and_drops_undone(self):
        got = db.rows("SELECT item_type, rating FROM review_log "
                      "WHERE subject = 'physics' ORDER BY item_type, log_id")
        self.assertEqual(len(got), LOGS)
        # 题目 reviews.result 是文本，必须统一 CAST 成整数评分
        self.assertEqual([r["rating"] for r in got if r["item_type"] == "problem"], [3])
        ratings = sorted(r["rating"] for r in got if r["item_type"] == "card")
        self.assertEqual(ratings, [3, 3, 4])   # 作废的 1 分不出现
        self.assertNotIn(1, ratings)


class TestDashboardCaliber(ReviewModelTestBase):
    """仪表盘：due 保持题目口径（与复习页队列同数），卡片另给字段。"""

    def test_due_is_problem_only_and_cards_are_separate(self):
        data = reports.dashboard("physics")
        self.assertEqual(data["due"], 1)
        self.assertEqual(data["due_cards"], 1)
        self.assertEqual(data["due_total"], 2)
        self.assertEqual(data["due_by_type"], {"problem": 1, "card": 1})

    def test_due_matches_progress_due_today(self):
        """同一个数：仪表盘 due 与进步面板 due_today 不能各算一套。"""
        self.assertEqual(reports.dashboard("physics")["due"],
                         reports.progress("physics")["due_today"])


class TestPressureAndTasks(ReviewModelTestBase):
    def test_pressure_counts_problems_and_cards_and_breaks_down_by_type(self):
        pressure = reports.pressure_index("physics")
        self.assertEqual(pressure["total"], 2)      # 今日：1 题 + 1 卡
        self.assertEqual(pressure["overdue"], 0)
        self.assertEqual(pressure["by_type"]["problem"]["today"], 1)
        self.assertEqual(pressure["by_type"]["card"]["today"], 1)

    def test_today_tasks_flags_that_cards_are_included(self):
        tasks = reports.today_tasks("physics")
        first = tasks[0]
        self.assertEqual(first["kind"], "review")
        self.assertEqual(first["count"], 2)
        self.assertIn("含卡片", first["label"])


class TestForgettingSurfaces(ReviewModelTestBase):
    def test_forget_predict_marks_item_type_and_no_problem_id_for_cards(self):
        predict = reports.forget_predict("physics")
        top = predict["top"]
        self.assertTrue(top)
        cards = [t for t in top if t["item_type"] == "card"]
        self.assertTrue(cards, "到期卡必须进入遗忘预测")
        # 卡片没有题目详情页可跳：problem_id 必须是 None，不能塞卡片 id（会跳错题）
        self.assertTrue(all(t["problem_id"] is None for t in cards))
        self.assertTrue(all(t["problem_id"] == t["item_id"]
                            for t in top if t["item_type"] == "problem"))

    def test_forgetting_curve_averages_over_cards_too(self):
        curve = reports.forgetting_curve(TODAY, "physics")
        # 题目 10.0 + 卡片 12.0 + 卡片 15.0 = 12.33；只算题目会得到 10.0
        self.assertEqual(curve["avg_stability"], 12.33)
        self.assertTrue(curve["curve"])

    def test_mature_share_uses_all_reviewable_items_as_denominator(self):
        """结构性指标的分母是全部可复习项（含从未排过的题目）——正是 INNER JOIN 踩过的坑。"""
        # 只有「到期题」repetition>=3 → 1/5；若分母漏掉无待复习行的题目会变成 1/4=0.25
        self.assertEqual(reports.north_star("physics")["mature_share"], 0.2)


class TestActivityLog(ReviewModelTestBase):
    """学习行为量：卡片复习必须计入周报/月报/学习者档案。"""

    def test_weekly_report_counts_card_reviews(self):
        week = reports.weekly_report("physics")
        self.assertEqual(week["week_reviews"], 1)   # 只有今天的卡片复习落在本周
        self.assertEqual(week["good_rate"], 1.0)

    def test_monthly_report_counts_card_reviews(self):
        month = reports.monthly_report("physics")
        self.assertEqual(month["month_revs"], 2)    # 今天 + 20 天前，两条都是卡片
        self.assertEqual(month["active_days"], 2)

    def test_profile_pace_counts_card_reviews(self):
        pace = profile.aggregate()["pace"]
        self.assertEqual(pace["week_reviews"], 1)
        self.assertEqual(pace["month_reviews"], 2)


class TestNorthStarIncludesCards(ReviewModelTestBase):
    def test_card_reviews_form_samples(self):
        """到期的卡有「前次复习」，因此构成一个样本；漏掉卡片会退回 nsNoData。"""
        ns = reports.north_star("physics")
        self.assertEqual(ns["reviews"], 1)
        self.assertEqual(ns["actual_retention"], 1.0)
        # 20 天前隔了 20 天才复习，而当时排定间隔 5 天 → 算逾期，不计入按时
        self.assertEqual(ns["on_time_rate"], 0.0)
        self.assertIsNone(ns["on_time_retention"])
        self.assertEqual(ns["overdue_retention"], 1.0)
        self.assertEqual(ns["advice_key"], "report.nsThin")   # 样本不足，只给数字不给结论


class TestAgentRules(ReviewModelTestBase):
    def test_problem_and_card_entries_are_separate_actions(self):
        plan = agent_rules.orchestrate("physics")
        actions = [s["action"] for s in plan["suggestions"]]
        self.assertIn("复习 1 道到期错题", actions)
        self.assertIn("复习 1 张到期闪卡", actions)

    def test_ctx_due_cards_excludes_problems(self):
        ctx = agent_rules._ctx("physics")
        self.assertEqual(ctx["due"], 1)
        self.assertEqual(ctx["due_cards"], 1)


if __name__ == "__main__":
    unittest.main()
