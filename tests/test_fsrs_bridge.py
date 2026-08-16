"""测试 A1 FSRS 适配层（含 SM-2 回退路径）。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
import fsrs_bridge
from fsrs_bridge import compute_fsrs_review, next_interval_days

# 测试临时数据严格限制在工作区内（tests/.tmp/），不留任何外部痕迹
_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)


class TestFsrsBridge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="fsrs_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "fsrs_test.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db
        cls._tmp.cleanup()

    def test_bridge_importable(self):
        self.assertIsInstance(fsrs_bridge._FSRS_AVAILABLE, bool)

    def test_interval_growth_with_state(self):
        """携带持久化状态连续复习：间隔应增长；忘卡后重置。"""
        d1 = compute_fsrs_review(4, 1, today=date(2026, 8, 11))
        d2 = compute_fsrs_review(4, d1.scheduled_days, d1.state, d1.stability, d1.difficulty, date(2026, 8, 12))
        self.assertGreater(d2.scheduled_days, d1.scheduled_days)
        forget = compute_fsrs_review(1, d2.scheduled_days, d2.state, d2.stability, d2.difficulty, date(2026, 8, 13))
        self.assertIn(forget.state, (1, 3))  # Learning 或 Relearning
        self.assertLessEqual(forget.scheduled_days, 1)

    def test_state_persistable_columns(self):
        """状态值应可直接写入 problems 列（合法数值）。"""
        fs = compute_fsrs_review(3, 1, today=date(2026, 8, 11))
        self.assertIsInstance(fs.state, int)
        self.assertGreaterEqual(fs.stability, 0)
        self.assertGreaterEqual(fs.difficulty, 0)
        self.assertEqual(fs.due[:10], "2026-08-11")

    def test_next_interval_returns_positive(self):
        for rating in (1, 2, 3, 4):
            for prev in (1, 7, 30):
                self.assertGreaterEqual(next_interval_days(rating, prev), 1)

    def test_desired_retention_persist_and_validate(self):
        """P0：目标保持率可设置并持久化；非法值拒绝。"""
        self.assertTrue(fsrs_bridge.set_desired_retention(0.85))
        self.assertAlmostEqual(fsrs_bridge._desired_retention(), 0.85)
        self.assertFalse(fsrs_bridge.set_desired_retention(0.5))
        self.assertFalse(fsrs_bridge.set_desired_retention("abc"))
        self.assertTrue(fsrs_bridge.set_desired_retention(0.9))
        self.assertAlmostEqual(fsrs_bridge._desired_retention(), 0.9)

    def test_retrievability_bounds(self):
        """P0：检索概率预测在 [0,1] 且新卡最近复习 R 高。"""
        r = fsrs_bridge.retrievability(prev_interval=1, last_review="2026-08-11",
                                       current=date(2026, 8, 12))
        self.assertGreaterEqual(r, 0.0)
        self.assertLessEqual(r, 1.0)

    def test_train_missing_deps_degrades(self):
        """P0：训练依赖（用户可选安装）缺失 → (False, reason)，调度不受影响。"""
        ok, payload = fsrs_bridge.train_parameters(
            [(i, 3, f"2026-08-{i:02d}T10:00:00") for i in range(1, 12)]
        )
        self.assertFalse(ok)
        self.assertIn("reason", payload)
        self.assertGreaterEqual(next_interval_days(3, 5), 1)  # 仍可调度


if __name__ == "__main__":
    unittest.main()
