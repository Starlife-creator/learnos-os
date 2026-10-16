"""#13 北极星指标：reports.north_star —— 实际保持率 vs 目标保持率（校准缺口）。

守卫的是「口径」而不是实现：首次接触不算保持、只统计有前次复习的样本、
按时/逾期分开看、样本不足不硬给结论。
"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
import fsrs_bridge
import reports

_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)
TODAY = date.today()


def _day(days_ago: int) -> str:
    return (TODAY - timedelta(days=days_ago)).isoformat() + "T09:00:00"


class NorthStarTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="north_star_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "north_star.db"
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
            conn.execute("DELETE FROM reviews")
            conn.execute("DELETE FROM problems")
            conn.execute("DELETE FROM settings WHERE key LIKE 'fsrs_desired_retention%'")

    def _problem(self, title: str, mastery: int = 3, repetition: int = 0) -> int:
        with db.DB_LOCK, db.db() as conn:
            cur = conn.execute(
                "INSERT INTO problems (title, content, subject, mastery, repetition, created_at, updated_at) "
                "VALUES (?, ?, 'physics', ?, ?, ?, ?)",
                (title, f"{title} 题干", mastery, repetition, _day(90), _day(0)),
            )
            return int(cur.lastrowid)

    def _review(self, pid: int, days_ago: int, interval: int, result: int) -> None:
        with db.DB_LOCK, db.db() as conn:
            conn.execute(
                "INSERT INTO reviews (problem_id, due_date, interval_days, completed, result, created_at) "
                "VALUES (?, ?, ?, 1, ?, ?)",
                (pid, _day(days_ago)[:10], interval, str(result), _day(days_ago)),
            )


class TestNorthStarCaliber(NorthStarTestBase):
    def test_first_exposure_is_not_retention(self):
        """首次接触某题（无前次复习）不算保持样本，否则「新题低分」会污染指标。"""
        pid = self._problem("A")
        self._review(pid, 50, 1, 1)
        data = reports.north_star("physics", days=60)
        self.assertEqual(data["reviews"], 0)
        self.assertIsNone(data["actual_retention"])
        self.assertEqual(data["verdict"], "insufficient")
        self.assertEqual(data["advice_key"], "report.nsNoData")

    def test_retention_and_on_time_split(self):
        a = self._problem("A")
        self._review(a, 50, 1, 3)    # 首次：排除
        self._review(a, 40, 7, 4)    # elapsed 10 > 7 → 逾期、成功
        self._review(a, 20, 10, 2)   # elapsed 20 > 10 → 逾期、失败
        b = self._problem("B")
        self._review(b, 45, 1, 3)    # 首次：排除
        self._review(b, 40, 7, 5)    # elapsed 5 ≤ 7 → 按时、成功
        self._review(b, 10, 5, 1)    # elapsed 30 > 5 → 逾期、失败

        data = reports.north_star("physics", days=60)
        self.assertEqual(data["reviews"], 4)
        self.assertEqual(data["actual_retention"], 0.5)
        self.assertEqual(data["on_time_rate"], 0.25)          # 1/4
        self.assertEqual(data["on_time_retention"], 1.0)
        self.assertEqual(data["overdue_retention"], 0.333)    # 1/3
        self.assertEqual(sum(b["count"] for b in data["by_horizon"]), 4)

    def test_window_excludes_old_samples_but_still_uses_them_as_prev(self):
        """窗口外的复习不计数，但仍是窗口内第一条的「前次」——否则首条会被误判成首次。"""
        pid = self._problem("A")
        self._review(pid, 90, 1, 3)
        self._review(pid, 25, 30, 4)   # elapsed 65：窗口 30 天内首条
        data = reports.north_star("physics", days=30)
        self.assertEqual(data["reviews"], 1)
        self.assertEqual(data["actual_retention"], 1.0)

    def test_horizon_buckets_are_cumulative_over_samples(self):
        pid = self._problem("A")
        prev = 80
        self._review(pid, prev, 1, 3)
        for gap in (2, 6, 10, 20, 45):
            self._review(pid, prev - gap, max(1, gap // 2), 3)
            prev -= gap
        data = reports.north_star("physics", days=120)
        labels = [b["label"] for b in data["by_horizon"]]
        self.assertEqual(labels, ["0-3天", "4-7天", "8-14天", "15-30天", "31天+"])
        self.assertEqual([b["count"] for b in data["by_horizon"]], [1, 1, 1, 1, 1])
        self.assertTrue(all(b["retention"] == 1.0 for b in data["by_horizon"]))


class TestNorthStarVerdict(NorthStarTestBase):
    """结论要基于足够样本；<20 一律「样本不足」，不硬给建议。"""

    def _bulk(self, total: int, good: int, interval: int = 10) -> None:
        pid = self._problem("bulk")
        self._review(pid, 200, 1, 3)          # 首次，排除
        base = 190
        for i in range(total):
            self._review(pid, base - i * 2, interval, 4 if i < good else 1)

    def test_thin_sample_stays_inconclusive(self):
        self._bulk(19, 19)
        data = reports.north_star("physics", days=365)
        self.assertEqual(data["verdict"], "insufficient")
        self.assertEqual(data["advice_key"], "report.nsThin")
        self.assertEqual(data["actual_retention"], 1.0)   # 数字照给，结论不给

    def test_conservative_when_above_target(self):
        self._bulk(24, 24)
        data = reports.north_star("physics", days=365)
        self.assertEqual(data["verdict"], "conservative")
        self.assertEqual(data["gap"], 0.1)

    def test_aggressive_when_below_target(self):
        self._bulk(24, 2)
        data = reports.north_star("physics", days=365)
        self.assertEqual(data["verdict"], "aggressive")
        self.assertLess(data["gap"], -0.08)

    def test_calibrated_near_target(self):
        self._bulk(24, 22)
        data = reports.north_star("physics", days=365)
        self.assertEqual(data["verdict"], "calibrated")
        self.assertAlmostEqual(data["actual_retention"], 0.917, places=3)


class TestNorthStarTarget(NorthStarTestBase):
    def test_target_comes_from_desired_retention(self):
        self.assertTrue(fsrs_bridge.set_desired_retention(0.85, "physics"))
        data = reports.north_star("physics", days=30)
        self.assertEqual(data["target_retention"], 0.85)

    def test_mature_share_is_structural(self):
        self._problem("new1", repetition=0)
        self._problem("new2", repetition=1)
        self._problem("grown1", repetition=3)
        self._problem("grown2", repetition=9)
        data = reports.north_star("physics", days=30)
        self.assertEqual(data["mature_share"], 0.5)

    def test_days_is_clamped(self):
        self.assertEqual(reports.north_star("physics", days=0)["window_days"], 1)
        self.assertEqual(reports.north_star("physics", days=9999)["window_days"], 365)


if __name__ == "__main__":
    unittest.main()
