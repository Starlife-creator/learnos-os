"""AI 审题 / AI 评分（含离线降级路径）测试。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
import ai
import bank

_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)


def _force_offline():
    """确保 AI 完全离线：清空内存密钥 + 伪造 ai_configured()=False。"""
    ai.set_runtime_key(None)
    ai.set_master_password(None)
    ai.ai_configured = lambda: False  # 屏蔽本机 keys.enc 干扰


def _subj_item(**kw) -> dict:
    base = {
        "type": "subjective",
        "stem": "请简述牛顿第二定律的物理意义。",
        "answer": "F=ma，加速度与合外力成正比、与质量成反比，方向同合外力。",
        "explain": "要点：比例关系、方向、质量=惯性量度。",
    }
    base.update(kw)
    return base


class TestReviewBankQuestion(unittest.TestCase):
    def setUp(self):
        _force_offline()

    def test_offline_degrades_pass(self):
        res = ai.review_bank_question({"type": "single", "stem": "x", "choices": ["A", "B"]})
        self.assertEqual(res["verdict"], "pass")
        self.assertFalse(res["ai_available"])
        self.assertEqual(res["issues"], [])

    def test_offline_no_crash_empty_question(self):
        res = ai.review_bank_question({})
        self.assertEqual(res["verdict"], "pass")
        self.assertFalse(res["ai_available"])


class TestAiScoreItem(unittest.TestCase):
    def setUp(self):
        _force_offline()

    def test_objective_single(self):
        it = {"type": "single", "stem": "题干足够长", "choices": ["A", "B", "C"], "answer": 1}
        r = ai.ai_score_item(it, 1)
        self.assertEqual(r["score"], 100)
        self.assertFalse(r["ai_available"])
        self.assertFalse(r["needs_review"])

    def test_objective_single_wrong(self):
        it = {"type": "single", "stem": "题干足够长", "choices": ["A", "B", "C"], "answer": 1}
        r = ai.ai_score_item(it, 0)
        self.assertEqual(r["score"], 0)

    def test_subjective_offline_needs_review(self):
        it = _subj_item()
        r = ai.ai_score_item(it, "我的作答")
        self.assertIsNone(r["score"])
        self.assertTrue(r["needs_review"])
        self.assertFalse(r["ai_available"])
        self.assertEqual(r["mode"], "unrated")

    def test_composite_offline_ignores_unrated_subjective_weight(self):
        it = {
            "type": "composite", "stem": "解答下列小题：",
            "parts": [
                {"type": "single", "stem": "（1）", "choices": ["A", "B"], "answer": 0},
                {"type": "subjective", "stem": "（2）", "answer": "要点"},
            ],
        }
        r = ai.ai_score_item(it, [0, "作答"])
        # 主观未评分 → 不拉低平均；只统计第1小问（正确=100）
        self.assertEqual(r["score"], 100)
        self.assertTrue(r["needs_review"])
        self.assertEqual(r["mode"], "unrated")

    def test_empty_answer_subjective_offline(self):
        # 未作答：离线下仍是 needs_review（不自动判 0 分，避免误伤）
        it = _subj_item()
        r = ai.ai_score_item(it, "")
        self.assertIsNone(r["score"])
        self.assertTrue(r["needs_review"])


if __name__ == "__main__":
    unittest.main()