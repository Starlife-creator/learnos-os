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

    def test_params_hash_stable_and_distinct(self):
        """F2 参数指纹：同参数同哈希、异参数异哈希、定长 12 位 hex。"""
        a = fsrs_bridge.params_hash([1.0, 2.0, 3.0])
        self.assertEqual(a, fsrs_bridge.params_hash([1.0, 2.0, 3.0]))
        self.assertNotEqual(a, fsrs_bridge.params_hash([1.0, 2.0, 3.1]))
        self.assertEqual(len(a), 12)
        int(a, 16)  # 合法 hex

    def test_load_params_computes_hash_fallback(self):
        """F2：写带 params_hash 的参数文件 → _load_params 可读；旧文件无哈希 → 现算兜底。"""
        import json as _json
        orig_file = fsrs_bridge._PARAM_FILE
        try:
            with tempfile.TemporaryDirectory(prefix="fsrs_hash_", dir=_TMP) as td:
                # 1) 新格式（train_parameters 产物）：哈希原样读出
                p1 = Path(td) / "params1.json"
                fsrs_bridge._PARAM_FILE = p1
                fsrs_bridge._invalidate()
                rounded = [round(float(p), 6) for p in fsrs_bridge.DEFAULT_PARAMETERS]
                p1.write_text(_json.dumps({
                    "parameters": rounded, "trained_at": "2026-09-04T10:00:00",
                    "params_hash": fsrs_bridge.params_hash(rounded),
                }), "utf-8")
                loaded = fsrs_bridge._load_params()
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded["params_hash"], fsrs_bridge.params_hash(rounded))
                # fsrs_status 暴露指纹（F2 验收）
                self.assertEqual(fsrs_bridge.fsrs_status()["params_hash"],
                                 fsrs_bridge.params_hash(rounded))
                # 2) 旧格式（无 params_hash）：按参数向量现算，确定性等价
                p2 = Path(td) / "params2.json"
                fsrs_bridge._PARAM_FILE = p2
                fsrs_bridge._invalidate()
                p2.write_text(_json.dumps({
                    "parameters": rounded, "trained_at": "2026-08-01T10:00:00",
                }), "utf-8")
                loaded2 = fsrs_bridge._load_params()
                self.assertEqual(loaded2["params_hash"], fsrs_bridge.params_hash(rounded))
        finally:
            fsrs_bridge._PARAM_FILE = orig_file
            fsrs_bridge._invalidate()


class TestFsrsPerSubject(unittest.TestCase):
    """§16.2 训练门槛单一常量 + 高置信度；§46.5C 学科分层保持率。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="fsrs_subj_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "fsrs_subj.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db
        cls._tmp.cleanup()

    def test_train_min_samples_constant(self):
        self.assertEqual(fsrs_bridge.FSRS_TRAIN_MIN_SAMPLES, 10)

    def test_confidence_bands(self):
        self.assertEqual(fsrs_bridge.confidence_for(9), "insufficient")
        self.assertEqual(fsrs_bridge.confidence_for(30), "low")
        self.assertEqual(fsrs_bridge.confidence_for(80), "medium")
        self.assertEqual(fsrs_bridge.confidence_for(300), "high")

    def test_per_subject_retention_override(self):
        # 全局默认 0.9；为 chemistry 设 0.85，math 不设 → 回退全局
        self.assertEqual(fsrs_bridge._desired_retention("chemistry"), 0.9)
        self.assertTrue(fsrs_bridge.set_desired_retention(0.85, subject="chemistry"))
        self.assertEqual(fsrs_bridge._desired_retention("chemistry"), 0.85)
        self.assertEqual(fsrs_bridge._desired_retention("math"), 0.9)  # 未设回退全局
        # 学科专属键正确写入 settings
        self.assertIn("fsrs_desired_retention_chemistry", fsrs_bridge.per_subject_retentions())
        # physics 视为全局等价键
        self.assertEqual(fsrs_bridge._retention_key("physics"), "fsrs_desired_retention")

    def test_set_desired_retention_rejects_out_of_range(self):
        self.assertFalse(fsrs_bridge.set_desired_retention(0.5, subject="math"))
        self.assertFalse(fsrs_bridge.set_desired_retention(0.99, subject="math"))

    def test_train_parameters_insufficient_uses_constant(self):
        """不足门槛 → 携带 confidence=insufficient，且用单一常量判定。"""
        ok, payload = fsrs_bridge.train_parameters(
            [(i, 3, f"2026-08-{i:02d}T10:00:00") for i in range(1, 9)]  # 8 < 10
        )
        self.assertFalse(ok)
        self.assertEqual(payload["sample_count"], 8)
        self.assertEqual(payload["confidence"], "insufficient")


class TestFamiliarityLabel(unittest.TestCase):
    """B6 P2-1：熟悉度词表 — 4 档阈值（R<0.5 hazy / <0.75 shaky / <0.9 familiar / ≥0.9 solid）。"""

    def test_thresholds(self):
        cases = [
            (-0.1, fsrs_bridge.FAM_HAZY),
            (0.0, fsrs_bridge.FAM_HAZY),
            (0.49999, fsrs_bridge.FAM_HAZY),
            (0.5, fsrs_bridge.FAM_SHAKY),
            (0.7, fsrs_bridge.FAM_SHAKY),
            (0.74999, fsrs_bridge.FAM_SHAKY),
            (0.75, fsrs_bridge.FAM_FAMILIAR),
            (0.85, fsrs_bridge.FAM_FAMILIAR),
            (0.89999, fsrs_bridge.FAM_FAMILIAR),
            (0.9, fsrs_bridge.FAM_SOLID),
            (0.99, fsrs_bridge.FAM_SOLID),
            (1.0, fsrs_bridge.FAM_SOLID),
        ]
        for R, expected in cases:
            with self.subTest(R=R):
                self.assertEqual(fsrs_bridge.familiarity_label(R), expected)

    def test_robust_to_bad_input(self):
        self.assertEqual(fsrs_bridge.familiarity_label(None), fsrs_bridge.FAM_HAZY)
        self.assertEqual(fsrs_bridge.familiarity_label("not a number"), fsrs_bridge.FAM_HAZY)
        self.assertEqual(fsrs_bridge.familiarity_label(float("nan")), fsrs_bridge.FAM_HAZY)

    def test_keys_match_i18n_suffix_pattern(self):
        """约定：4 个键小写，对应 i18n 键 fsrs.famHazy/famShaky/famFamiliar/famSolid。"""
        from fsrs_bridge import FAM_HAZY, FAM_SHAKY, FAM_FAMILIAR, FAM_SOLID
        self.assertEqual(FAM_HAZY, "hazy")
        self.assertEqual(FAM_SHAKY, "shaky")
        self.assertEqual(FAM_FAMILIAR, "familiar")
        self.assertEqual(FAM_SOLID, "solid")


if __name__ == "__main__":
    unittest.main()
