"""v1 学科感知 PR 专项测试：judge 非数值判分 + 变式/费曼学科化（离线/ mock，确定性）。"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
from bank import _grade_answer
from ai import generate_variants, fallback_hint, local_tags
from oral import start_feynman

_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)


class TestGradeAnswer(unittest.TestCase):
    """修复：原 judge 强制 int()，非数值学科一律判错或崩溃。"""

    def test_numeric_equal(self):
        self.assertTrue(_grade_answer("42", "42"))

    def test_numeric_float_equal(self):
        self.assertTrue(_grade_answer("3.5", "3.5"))

    def test_numeric_mismatch(self):
        self.assertFalse(_grade_answer("42", "43"))

    def test_numeric_tolerance(self):
        self.assertTrue(_grade_answer("1.000001", "1.0"))

    def test_numeric_thousands_separator(self):
        self.assertTrue(_grade_answer("1,000", "1000"))

    def test_non_numeric_equal(self):
        self.assertTrue(_grade_answer("真", "真"))

    def test_non_numeric_mismatch(self):
        self.assertFalse(_grade_answer("A", "B"))

    def test_string_normalization(self):
        # 去首尾标点 + 小写后应相等
        self.assertTrue(_grade_answer("  True。", "true"))

    def test_mixed_types_mismatch(self):
        # 数值无法与文本对应
        self.assertFalse(_grade_answer("5", "五"))


class TestGenerateVariantsSubject(unittest.TestCase):
    """变式生成：AI 不可用时回落本地模板，数学学科不应出现「量纲」等物理专属词。"""

    def test_math_local_no_dimension_word(self):
        problem = {
            "subject": "math", "course": "高等数学", "topic": "导数",
            "content": "已知函数 f(x)=x^2，求 f'(x)。", "title": "导数题",
            "error_type": "calculation",
        }
        with mock.patch("ai.call_ai", side_effect=RuntimeError("no network")):
            source, variants = generate_variants(problem)
        self.assertEqual(source, "local")
        self.assertTrue(variants)
        joined = " ".join(v.get("answer", "") for v in variants)
        self.assertNotIn("量纲", joined)
        self.assertIn("单位", joined)

    def test_physics_local_keeps_dimension_word(self):
        problem = {
            "subject": "physics", "course": "力学", "topic": "牛顿定律",
            "content": "质量 m=2kg 的物体受 F=10N 合力，求加速度。", "title": "力学题",
            "error_type": "calculation",
        }
        with mock.patch("ai.call_ai", side_effect=RuntimeError("no network")):
            source, variants = generate_variants(problem)
        self.assertEqual(source, "local")
        joined = " ".join(v.get("answer", "") for v in variants)
        self.assertIn("量纲", joined)


class TestFeynmanSubject(unittest.TestCase):
    """费曼口述反转：学科感知的新手讲解提示，非物理不含『物理』。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="feynman_subj_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "feynman_subj_test.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db
        try:
            cls._tmp.cleanup()
        except OSError:
            # 沙箱安全删除机制在无回收站时拒绝 rmtree，忽略（临时文件留在 tests/.tmp）
            pass

    def _make_problem(self, subject, topic):
        from db import now
        with db.db() as conn:
            cur = conn.execute(
                "INSERT INTO problems(title, content, topic, subject, course, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("错题", "概念讲解", topic, subject, topic, now(), now()),
            )
        return int(cur.lastrowid)

    def test_math_feynman_no_physics(self):
        pid = self._make_problem("math", "线性代数")
        problem = db.row("SELECT * FROM problems WHERE id = ?", (pid,))
        with mock.patch("ai.call_ai", side_effect=RuntimeError("no network")):
            sid, q = start_feynman(problem)
        self.assertNotIn("物理", q)

    def test_physics_feynman_mentions_physics(self):
        pid = self._make_problem("physics", "电磁感应")
        problem = db.row("SELECT * FROM problems WHERE id = ?", (pid,))
        with mock.patch("ai.call_ai", side_effect=RuntimeError("no network")):
            sid, q = start_feynman(problem)
        self.assertIn("物理", q)


class TestOfflineFallbackSubject(unittest.TestCase):
    """离线兜底（无 AI 的默认本地模式）：非物理学科不应出现物理专属措辞。"""

    def test_math_fallback_no_physics_terms(self):
        problem = {
            "subject": "math", "course": "高等数学", "topic": "定积分",
            "content": "计算 ∫₀¹ x² dx", "my_attempt": "",
        }
        for lvl in (1, 2, 3, 4):
            hint = fallback_hint(problem, lvl)
            self.assertNotIn("物理模型", hint)
            self.assertNotIn("受力图", hint)
            self.assertNotIn("量纲", hint)

    def test_math_fallback_zh_mentions_topic(self):
        problem = {
            "subject": "math", "course": "高等数学", "topic": "定积分",
            "content": "计算 ∫₀¹ x² dx", "my_attempt": "用牛顿-莱布尼茨公式",
        }
        hint = fallback_hint(problem, 2)
        self.assertIn("定积分", hint)

    def test_physics_fallback_keeps_physics_terms(self):
        # 物理（或未知学科）保持原措辞，向后兼容既有物理流程与测试。
        # 通用物理（无细分分支匹配）的兜底框架含「物理模型」，区别于中性通用提示。
        problem = {
            "subject": "physics", "course": "物理", "topic": "功和能",
            "content": "计算变力做功", "my_attempt": "",
        }
        hint = fallback_hint(problem, 4)
        self.assertIn("物理模型", hint)

    def test_unknown_subject_falls_back_to_physics(self):
        # 未指定学科时回落到物理默认框架（含「物理模型」），而非中性通用提示。
        problem = {
            "course": "物理", "topic": "功和能",
            "content": "计算变力做功", "my_attempt": "",
        }
        hint = fallback_hint(problem, 4)
        self.assertIn("物理模型", hint)


class TestLocalTagsSubject(unittest.TestCase):
    """离线标签兜底：非物理学科不应被打上物理分科标签。"""

    def test_math_local_no_physics_knowledge_tag(self):
        tags = local_tags("导数题", "已知函数 f(x)=x^2，求 f'(x)。",
                          course="高等数学", topic="导数", subject="math")
        joined = " ".join(tags["tags"])
        self.assertNotIn("电磁学", joined)
        self.assertNotIn("力学", joined)
        self.assertIn("课程:高等数学", joined)

    def test_physics_local_keeps_physics_knowledge_tag(self):
        tags = local_tags("电磁感应题", "求线圈在磁场中产生的感应电动势。",
                          course="电磁学", topic="电磁感应", subject="physics")
        joined = " ".join(tags["tags"])
        self.assertIn("电磁学", joined)

    def test_unknown_subject_local_defaults_physics(self):
        tags = local_tags("电磁感应题", "求感应电动势。",
                          course="电磁学", topic="电磁感应")
        joined = " ".join(tags["tags"])
        self.assertIn("电磁学", joined)


if __name__ == "__main__":
    unittest.main()
