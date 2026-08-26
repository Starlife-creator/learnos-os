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

    def test_subjective_pending_third_state(self):
        """C6 第三态回归：主观题待评阅落 correct=2，状态记 pending，
        不再冒充 done；stats/units 的 todo 不被虚高。"""
        it = bank._normalize_question(
            {"type": "subjective", "id": "q-pend1", "stem": "题干足够长内容", "answer": "参考",
             "unit": "测试单元"},
            1, set(), "physics")
        self._inject(it)
        res = bank.judge("q-pend1", "我的答案", "physics")
        self.assertIsNone(res["correct"])
        with db.db() as conn:
            cval = conn.execute(
                "SELECT correct FROM bank_attempts WHERE qid='q-pend1' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        self.assertEqual(cval, 2, "主观待评阅应落第三态 2")
        st = bank.stats("physics")
        self.assertEqual(st["pending"], 1)
        self.assertEqual(st["done"], 0, "待评阅不得计入已掌握")
        self.assertEqual(st["todo"], max(0, st["total"] - st["done"] - st["wrong"] - st["pending"]))
        for u in bank.units("physics"):
            if u["unit"] == "测试单元":
                self.assertEqual(u["pending"], 1)
                break
        else:
            self.fail("未找到注入单元")
        items = bank.list_questions(status="pending", subject="physics")
        self.assertTrue(any(x["id"] == "q-pend1" for x in items), "pending 筛选应命中该题")
        # 状态机：末次记录决定状态（2→pending，随后答对 1→done）
        self.assertEqual(bank._status_of("q-pend1", {"q-pend1": [(2, "t1"), (1, "t2")]}), "done")
        self.assertEqual(bank._status_of("q-pend1", {"q-pend1": [(1, "t1"), (2, "t2")]}), "pending")

    def test_record_review_outcome_transitions(self):
        """C6 决策：AI 评分确定结果回写 attempts——≥60 记 done，<60 记 wrong 并入错题库，
        score=None / needs_review 跳过（保持 pending）。"""
        it = bank._normalize_question(
            {"type": "subjective", "id": "q-rev1", "stem": "题干足够长内容", "answer": "参考",
             "unit": "测试单元"},
            1, set(), "physics")
        self._inject(it)
        bank.judge("q-rev1", "我的答案", "physics")   # 落 pending（correct=2）
        self.assertEqual(bank._status_of("q-rev1", bank._attempt_stats()), "pending")
        # skip：AI 离线（score=None）
        self.assertEqual(bank.record_review_outcome("q-rev1", "physics", None, False), "skipped")
        self.assertEqual(bank._status_of("q-rev1", bank._attempt_stats()), "pending")
        # fail：45 分 → correct=0 且入错题库
        self.assertEqual(bank.record_review_outcome("q-rev1", "physics", 45, False), "fail")
        st = bank._attempt_stats()["q-rev1"]
        self.assertEqual(st[-1][0], 0)
        with db.db() as conn:
            archived = conn.execute(
                "SELECT problem_id FROM bank_problems WHERE qid='q-rev1'").fetchone()
        self.assertIsNotNone(archived, "评不及格应与 judge 同路径入错题库")
        self.assertEqual(bank._status_of("q-rev1", bank._attempt_stats()), "wrong")
        # pass：85 分 → correct=1，末次记录优先转 done
        self.assertEqual(bank.record_review_outcome("q-rev1", "physics", 85, False), "pass")
        st = bank._attempt_stats()["q-rev1"]
        self.assertEqual(st[-1][0], 1)
        self.assertEqual(bank._status_of("q-rev1", bank._attempt_stats()), "done")

    def test_judge_composite_wrong_archives_full_content(self):
        # P1：composite 答错建档须含完整题面（引导语+子题），标题不空
        it = bank._normalize_question(
            {"type": "composite", "id": "q-c1", "stem": "", "parts": [
                {"type": "single", "stem": "（1）加速度方向判断？", "choices": ["同向", "反向"], "answer": 0},
                {"type": "fill", "stem": "（2）末速度为 __ m/s", "answer": "10"},
            ]}, 1, set(), "physics")
        self._inject(it)
        res = bank.judge("q-c1", [1, "10"], "physics")  # 第一小题错
        self.assertFalse(res["correct"])
        self.assertGreater(res["problem_id"], 0)
        rows = db.rows("SELECT title, content FROM problems WHERE id = ?", (res["problem_id"],))
        self.assertEqual(len(rows), 1)
        self.assertIn("加速度方向判断", rows[0]["content"])
        self.assertIn("末速度", rows[0]["content"])
        self.assertIn("同向", rows[0]["content"])  # 选项也入档，复习时可见
        self.assertTrue(rows[0]["title"].strip())  # 标题兜底（引导语为空时取子题题干）

    def test_problem_content_single_with_choices(self):
        # 普通选择题建档 content 含题干+选项（复习时不必再回题库查）
        content = bank._problem_content(
            {"type": "single", "stem": "下列说法正确的是", "choices": ["甲", "乙"], "answer": 0})
        self.assertIn("下列说法正确的是", content)
        self.assertIn("A. 甲", content)


class TestBankListAPI(unittest.TestCase):
    """列表/搜索端点行为：blanks 下发不泄答案、搜索命中带题型。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="bankapi_", dir=_TMP)
        cls._orig = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "bankapi.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()
        db.DB_PATH = cls._orig
        config.DB_PATH = cls._orig

    def test_list_fill_blanks_no_answer(self):
        bank._BANK.pop("physics", None)
        bank._BANK["physics"] = {"version": 0, "subject": "physics", "questions": [
            {"type": "fill", "id": "fill-api-1", "stem": "两空填空题干内容", "answer": ["5", "15"],
             "unit": "u", "chapter": "c", "concept": "cp", "difficulty": 2, "explain": "e"},
        ]}
        items = bank.list_questions(subject="physics")
        self.assertEqual(len(items), 1)
        self.assertNotIn("answer", items[0])
        self.assertNotIn("explain", items[0])
        self.assertEqual(items[0]["blanks"], 2)
        bank._BANK.pop("physics", None)


class TestBankListStrip(unittest.TestCase):
    def test_pub_strips_answers_recursive(self):
        item = {"type": "composite", "id": "c1", "stem": "s",
                "parts": [{"type": "single", "id": "p1", "stem": "a",
                           "choices": ["A", "B"], "answer": 0, "explain": "e"}]}
        pub = bank._pub_item(item)
        self.assertNotIn("answer", pub)
        self.assertNotIn("explain", pub["parts"][0])
        self.assertNotIn("answer", pub["parts"][0])

    def test_pub_fill_blanks_count(self):
        # P0：fill 对外剥离答案但附带空数（前端据此渲染多空，不泄露答案）
        pub = bank._pub_item({"type": "fill", "id": "f1", "stem": "两空：__ __",
                              "answer": ["5", "15"], "explain": "e"})
        self.assertNotIn("answer", pub)
        self.assertEqual(pub["blanks"], 2)
        pub1 = bank._pub_item({"type": "fill", "id": "f2", "stem": "单空 __", "answer": "4"})
        self.assertEqual(pub1["blanks"], 1)
        # composite 子题 fill 同样附带
        pubc = bank._pub_item({"type": "composite", "id": "c1", "stem": "",
                               "parts": [{"type": "fill", "id": "p1", "stem": "x", "answer": ["a", "b"]}]})
        self.assertEqual(pubc["parts"][0]["blanks"], 2)
        self.assertNotIn("answer", pubc["parts"][0])


if __name__ == "__main__":
    unittest.main()
