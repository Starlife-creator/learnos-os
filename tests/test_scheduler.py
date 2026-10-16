"""scheduler.schedule_review 单元测试（ADR 0007：FSRS/SM-2 分轨）。"""
import unittest
from datetime import date, timedelta
from unittest import mock

import fsrs_bridge
from review import compute_review
from scheduler import schedule_review, overdue_days

TODAY = date(2026, 10, 16)


def _ago(days: int) -> str:
    return (TODAY - timedelta(days=days)).isoformat()


@unittest.skipUnless(fsrs_bridge.fsrs_available(), "FSRS vendored 依赖不可用")
class TestFsrsBranch(unittest.TestCase):
    def test_engine_and_due_consistent(self):
        r = schedule_review(3, prev_interval=1, today=TODAY)
        self.assertEqual(r.engine, "fsrs")
        self.assertEqual(r.next_due, (TODAY + timedelta(days=r.interval_days)).isoformat())

    def test_no_double_overdue_penalty(self):
        """FSRS 支不再叠加逾期顺延：逾期复习的间隔不会被再减半/重置为 1。"""
        mature = dict(prev_interval=10, state=2, stability=10.0, difficulty=5.0)
        on_time = schedule_review(3, due_date=_ago(0), last_review=_ago(10), today=TODAY, **mature)
        late = schedule_review(3, due_date=_ago(25), last_review=_ago(35), today=TODAY, **mature)
        self.assertEqual(late.engine, "fsrs")
        self.assertGreater(late.interval_days, 1)
        self.assertGreater(late.stability, 0)
        self.assertEqual(late.overdue_days, 25)
        # 记得住逾期很久的内容 → 稳定性增长不低于准时复习（FSRS 的「间隔效应」）
        self.assertGreaterEqual(late.stability, on_time.stability)

    def test_last_review_is_used(self):
        """修复前 card.last_review 从未设置（R 恒为 0），last_review 不同结果也相同。"""
        mature = dict(prev_interval=10, state=2, stability=10.0, difficulty=5.0, today=TODAY)
        soon = schedule_review(3, last_review=_ago(1), **mature)
        later = schedule_review(3, last_review=_ago(10), **mature)
        self.assertNotEqual(soon.stability, later.stability)

    def test_good_grows_interval(self):
        r1 = schedule_review(3, prev_interval=1, today=TODAY)
        r2 = schedule_review(3, prev_interval=r1.interval_days, state=r1.state,
                             stability=r1.stability, difficulty=r1.difficulty,
                             due_date=r1.next_due, last_review=TODAY.isoformat(),
                             today=TODAY + timedelta(days=r1.interval_days))
        self.assertGreaterEqual(r2.interval_days, r1.interval_days)

    def test_exception_falls_back_to_sm2(self):
        with mock.patch.object(fsrs_bridge, "compute_fsrs_review", side_effect=RuntimeError("x")):
            r = schedule_review(3, prev_interval=5, today=TODAY)
        self.assertEqual(r.engine, "sm2")


class TestSm2Branch(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(fsrs_bridge, "fsrs_available", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def test_matches_compute_review_when_on_time(self):
        r = schedule_review(3, prev_interval=3, prev_ease=2.5, prev_repetition=2,
                            due_date=_ago(0), today=TODAY)
        base = compute_review(3, 3, 2.5, 2)
        self.assertEqual(r.engine, "sm2")
        self.assertEqual((r.interval_days, r.ease_factor, r.repetition, r.mastery),
                         (base.interval_days, base.ease_factor, base.repetition, base.mastery))

    def test_overdue_halves(self):
        base = compute_review(3, 16, 2.5, 3)
        r = schedule_review(3, prev_interval=16, prev_repetition=3, due_date=_ago(10), today=TODAY)
        self.assertEqual(r.interval_days, max(1, base.interval_days // 2))

    def test_long_overdue_resets(self):
        r = schedule_review(3, prev_interval=30, prev_repetition=4, state=2, stability=20.0,
                            difficulty=5.0, due_date=_ago(22), today=TODAY)
        self.assertEqual((r.interval_days, r.repetition, r.mastery), (1, 0, 4))
        self.assertEqual((r.state, r.stability, r.difficulty), (0, 0.0, 0.0))

    def test_keeps_fsrs_state_when_not_reset(self):
        r = schedule_review(3, prev_interval=5, state=2, stability=7.5, difficulty=4.0,
                            due_date=_ago(0), today=TODAY)
        self.assertEqual((r.state, r.stability, r.difficulty), (2, 7.5, 4.0))


class TestOverdueDays(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(overdue_days(_ago(3), TODAY), 3)
        self.assertEqual(overdue_days(_ago(3) + " 10:00:00", TODAY), 3)
        self.assertEqual(overdue_days("", TODAY), 0)
        self.assertEqual(overdue_days("garbage", TODAY), 0)
        self.assertEqual(overdue_days((TODAY + timedelta(days=2)).isoformat(), TODAY), 0)


if __name__ == "__main__":
    unittest.main()
