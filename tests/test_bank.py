"""题库多题型（单选/多选/填空/主观/大小题）归一化、判分与归档测试。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
import bank

_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)


class TestBankNormalize(unittest.TestCase):
    def test_single_backcompat_no_type(self):
        it = bank._normalize_question(
            {"stem": "题干足够长内容", "choices": ["A", "B"], "answer": 0}, 1, set(), "physics")
        self.assertEqual(it["type"], "single")
        self.assertEqual(it["answer"], 0)

    def test_single_ok(self):
        it = bank._normalize_question(
            {"type": "single", "stem": "题干足够长内容", "choices": ["A", "B", "C"], "answer": 1},
            1, set(), "physics")
        self.assertEqual(it["type"], "single")
        self.assertEqual(it["answer"], 1)

    def test_single_bad_answer_index(self):
        with self.assertRaises(ValueError):
            bank._normalize_question(
                {"type": "single", "stem": "题干足够长内容", "choices": ["A", "B"], "answer": 5},
                1, set(), "physics")

    def test_multiple_ok(self):
        it = bank._normalize_question(
            {"type": "multiple", "stem": "题干足够长内容", "choices": ["A", "B", "C"], "answer": [0, 2]},
            1, set(), "physics")
        self.assertEqual(it["answer"], [0, 2])

    def test_multiple_letter_answer(self):
        it = bank._normalize_question(
            {"type": "multiple", "stem": "题干足够长内容", "choices": ["A", "B", "C"], "answer": "A,C"},
            1, set(), "physics")
        self.assertEqual(it["answer"], [0, 2])

    def test_multiple_bad_answer(self):
        with self.assertRaises(ValueError):
            bank._normalize_question(
                {"type": "multiple", "stem": "题干足够长内容", "choices": ["A", "B"], "answer": [0, 5]},
                1, set(), "physics")

    def test_fill_str(self):
        it = bank._normalize_question(
            {"type": "fill", "stem": "题干足够长内容", "answer": "答案"}, 1, set(), "physics")
        self.assertEqual(it["answer"], "答案")

    def test_fill_list(self):
        it = bank._normalize_question(
            {"type": "fill", "stem": "题干足够长内容", "answer": ["a", "b"]}, 1, set(), "physics")
        self.assertEqual(it["answer"], ["a", "b"])

    def test_fill_empty(self):
        with self.assertRaises(ValueError):
            bank._normalize_question(
                {"type": "fill", "stem": "题干足够长内容", "answer": ""}, 1, set(), "physics")

    def test_subjective_ok(self):
        it = bank._normalize_question(
            {"type": "subjective", "stem": "题干足够长内容", "answer": "参考答案文本"}, 1, set(), "physics")
        self.assertEqual(it["type"], "subjective")

    def test_subjective_empty(self):
        with self.assertRaises(ValueError):
            bank._normalize_question(
                {"type": "subjective", "stem": "题干足够长内容", "answer": ""}, 1, set(), "physics")

    def test_composite_ok(self):
        it = bank._normalize_question(
            {"type": "composite", "stem": "解答：", "parts": [
                {"type": "single", "stem": "（1）子题足够长内容", "choices": ["A", "B"], "answer": 0},
                {"type": "fill", "stem": "（2）填空足够长内容", "answer": "x"},
            ]}, 1, set(), "physics")
        self.assertEqual(len(it["parts"]), 2)
        self.assertEqual(it["parts"][1]["type"], "fill")

    def test_composite_requires_parts(self):
        with self.assertRaises(ValueError):
            bank._normalize_question(
                {"type": "composite", "stem": "解答：", "parts": []}, 1, set(), "physics")

    def test_composite_nested(self):
        it = bank._normalize_question(
            {"type": "composite", "stem": "外层", "parts": [
                {"type": "composite", "stem": "中层", "parts": [
                    {"type": "single", "stem": "内层子题足够长", "choices": ["A", "B"], "answer": 0},
                ]},
            ]}, 1, set(), "physics")
        self.assertEqual(it["parts"][0]["type"], "composite")
        self.assertEqual(it["parts"][0]["parts"][0]["type"], "single")

    def test_unknown_type(self):
        with self.assertRaises(ValueError):
            bank._normalize_question(
                {"type": "weird", "stem": "题干足够长内容", "choices": ["A", "B"], "answer": 0},
                1, set(), "physics")


class TestBankGrading(unittest.TestCase):
    def test_single_correct_wrong(self):
        it = {"type": "single", "stem": "s", "choices": ["A", "B"], "answer": 0}
        self.assertTrue(bank.grade_item(it, 0)["correct"])
        self.assertFalse(bank.grade_item(it, 1)["correct"])

    def test_multiple(self):
        it = {"type": "multiple", "stem": "s", "choices": ["A", "B", "C"], "answer": [0, 2]}
        self.assertTrue(bank.grade_item(it, [0, 2])["correct"])
        self.assertFalse(bank.grade_item(it, [0, 1])["correct"])
        self.assertFalse(bank.grade_item(it, [0])["correct"])

    def test_fill_single(self):
        it = {"type": "fill", "stem": "s", "answer": "5"}
        self.assertTrue(bank.grade_item(it, "5")["correct"])
        self.assertFalse(bank.grade_item(it, "6")["correct"])
        # 归一化：忽略首尾空白/标点、忽略大小写
        self.assertTrue(bank.grade_item(it, " 5。")["correct"])

    def test_fill_multi(self):
        it = {"type": "fill", "stem": "s", "answer": ["5", "15"]}
        self.assertTrue(bank.grade_item(it, ["5", "15"])["correct"])
        self.assertFalse(bank.grade_item(it, ["5", "16"])["correct"])
        self.assertFalse(bank.grade_item(it, ["5"])["correct"])

    def test_subjective_needs_review(self):
        it = {"type": "subjective", "stem": "s", "answer": "参考"}
        r = bank.grade_item(it, "我的作答")
        self.assertIsNone(r["correct"])
        self.assertTrue(r["needs_review"])

    def test_composite_recursive_with_subjective(self):
        it = {"type": "composite", "stem": "s", "parts": [
            {"type": "single", "stem": "a", "choices": ["A", "B"], "answer": 0},
            {"type": "subjective", "stem": "b", "answer": "ref"},
        ]}
        r = bank.grade_item(it, [0, "作答"])
        self.assertIsNone(r["correct"])  # 含主观 → 待评阅
        self.assertTrue(r["needs_review"])
        self.assertEqual(len(r["parts"]), 2)

    def test_composite_wrong(self):
        it = {"type": "composite", "stem": "s", "parts": [
            {"type": "single", "stem": "a", "choices": ["A", "B"], "answer": 0},
            {"type": "fill", "stem": "b", "answer": "x"},
        ]}
        r = bank.grade_item(it, [1, "x"])  # 第一小题错
        self.assertFalse(r["correct"])
        self.assertFalse(r["needs_review"])


class TestBankJudgeDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="bank_", dir=_TMP)
        cls._orig = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "bank_test.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()
        db.DB_PATH = cls._orig
        config.DB_PATH = cls._orig

    def _inject(self, item):
        bank._BANK["physics"] = {"version": 0, "subject": "physics", "questions": [item]}

    def test_judge_single_wrong_archives(self):
        it = bank._normalize_question(
            {"type": "single", "id": "q-s1", "stem": "题干足够长内容", "choices": ["A", "B"], "answer": 0},
            1, set(), "physics")
        self._inject(it)
        res = bank.judge("q-s1", 1, "physics")
        self.assertFalse(res["correct"])
        self.assertGreater(res["problem_id"], 0)  # 答错入错题库

    def test_judge_subjective_no_archive(self):
        it = bank._normalize_question(
            {"type": "subjective", "id": "q-sub1", "stem": "题干足够长内容", "answer": "参考"},
            1, set(), "physics")
        self._inject(it)
        res = bank.judge("q-sub1", "我的答案", "physics")
        self.assertIsNone(res["correct"])
        self.assertTrue(res["needs_review"])
        self.assertEqual(res["problem_id"], 0)  # 主观不自动入错题库


class TestBankListStrip(unittest.TestCase):
    def test_pub_strips_answers_recursive(self):
        item = {"type": "composite", "id": "c1", "stem": "s",
                "parts": [{"type": "single", "id": "p1", "stem": "a",
                           "choices": ["A", "B"], "answer": 0, "explain": "e"}]}
        pub = bank._pub_item(item)
        self.assertNotIn("answer", pub)
        self.assertNotIn("explain", pub["parts"][0])
        self.assertNotIn("answer", pub["parts"][0])


if __name__ == "__main__":
    unittest.main()
