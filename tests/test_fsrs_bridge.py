"""测试 A1 FSRS 适配层（含 SM-2 回退路径）。"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fsrs_bridge
from fsrs_bridge import compute_fsrs_review, next_interval_days


class TestFsrsBridge(unittest.TestCase):
    def test_bridge_importable(self):
        self.assertIsInstance(fsrs_bridge._FSRS_AVAILABLE, bool)

    def test_interval_growth_with_state(self):
        """携带持久化状态连续复习：间隔应增长；忘卡后重置。"""
        if not fsrs_bridge._FSRS_AVAILABLE:
            self.skipTest("FSRS vendored 缺失")
        d1 = compute_fsrs_review(4, 1, today=date(2026, 8, 11))
        d2 = compute_fsrs_review(4, d1.scheduled_days, d1.state, d1.stability, d1.difficulty, date(2026, 8, 12))
        self.assertGreater(d2.scheduled_days, d1.scheduled_days)
        forget = compute_fsrs_review(1, d2.scheduled_days, d2.state, d2.stability, d2.difficulty, date(2026, 8, 13))
        self.assertIn(forget.state, (1, 3))  # Learning 或 Relearning
        self.assertLessEqual(forget.scheduled_days, 1)

    def test_state_persistable_columns(self):
        """状态值应可直接写入 problems 列（合法数值）。"""
        if not fsrs_bridge._FSRS_AVAILABLE:
            self.skipTest("FSRS vendored 缺失")
        fs = compute_fsrs_review(3, 1, today=date(2026, 8, 11))
        self.assertIsInstance(fs.state, int)
        self.assertGreaterEqual(fs.stability, 0)
        self.assertGreaterEqual(fs.difficulty, 0)
        self.assertEqual(fs.due[:10], "2026-08-11")

    def test_next_interval_returns_positive(self):
        for rating in (1, 2, 3, 4):
            for prev in (1, 7, 30):
                self.assertGreaterEqual(next_interval_days(rating, prev), 1)

    def test_sm2_fallback_when_unavailable(self):
        """模拟 vendored 缺失：next_interval_days 应仍返回合法值（SM-2 路径）。"""
        saved = fsrs_bridge._FSRS_AVAILABLE
        fsrs_bridge._FSRS_AVAILABLE = False
        try:
            for rating in (1, 2, 3, 4):
                self.assertGreaterEqual(next_interval_days(rating, 5), 1)
        finally:
            fsrs_bridge._FSRS_AVAILABLE = saved


if __name__ == "__main__":
    unittest.main()
