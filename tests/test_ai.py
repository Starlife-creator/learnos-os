"""测试 AI 相关函数：URL 拼接、降级提示、学科感知人格。"""
import tempfile
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ai import (
    api_endpoint, fallback_hint, problem_prompt, get_cached_settings, invalidate_settings_cache,
    _resolve_subject, _subject_profile, extract_tags,
)
import config
import db

# 测试临时数据严格限制在工作区内（tests/.tmp/），不留任何外部痕迹
_TEST_TMP_DIR = Path(__file__).resolve().parent / ".tmp"
_TEST_TMP_DIR.mkdir(exist_ok=True)


class TestApiEndpoint(unittest.TestCase):
    def test_append_chat_completions(self):
        self.assertEqual(api_endpoint("https://api.openai.com/v1"), "https://api.openai.com/v1/chat/completions")

    def test_already_has_path(self):
        url = "https://api.openai.com/v1/chat/completions"
        self.assertEqual(api_endpoint(url), url)

    def test_trailing_slash(self):
        self.assertEqual(api_endpoint("https://api.openai.com/v1/"), "https://api.openai.com/v1/chat/completions")

    def test_strips_whitespace(self):
        self.assertEqual(api_endpoint("  https://api.openai.com/v1  "), "https://api.openai.com/v1/chat/completions")

    def test_custom_base(self):
        self.assertEqual(api_endpoint("https://my-proxy.com/llm"), "https://my-proxy.com/llm/chat/completions")


class TestFallbackHint(unittest.TestCase):
    def setUp(self):
        self.problem = {
            "title": "Test",
            "course": "力学",
            "topic": "转动惯量",
            "content": "求均匀圆盘的转动惯量",
            "my_attempt": "",
        }

    def test_level_1_mentions_dimension(self):
        hint = fallback_hint(self.problem, 1)
        self.assertIn("受力图", hint)
        self.assertIn("转动惯量", hint)

    def test_level_2_mentions_steps(self):
        hint = fallback_hint(self.problem, 2)
        self.assertIn("运动方程", hint)

    def test_level_3_mentions_check(self):
        hint = fallback_hint(self.problem, 3)
        self.assertIn("量纲", hint)

    def test_level_2_with_attempt(self):
        self.problem["my_attempt"] = "I = 1/2 MR^2"
        hint = fallback_hint(self.problem, 2)
        self.assertIn("基本方程", hint)

    def test_no_topic(self):
        self.problem["topic"] = ""
        hint = fallback_hint(self.problem, 1)
        self.assertIn("这个问题", hint)


class TestProblemPrompt(unittest.TestCase):
    def setUp(self):
        self.problem = {
            "subject": "physics",
            "course": "电磁学",
            "topic": "高斯定理",
            "content": "求球壳外电场",
            "my_attempt": "",
        }

    def test_returns_two_messages(self):
        msgs = problem_prompt(self.problem, 1)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[1]["role"], "user")

    def test_system_role_is_physics_ta(self):
        msgs = problem_prompt(self.problem, 2)
        self.assertIn("物理助教", msgs[0]["content"])

    def test_level_included(self):
        msgs = problem_prompt(self.problem, 3)
        self.assertIn("第 3 级", msgs[1]["content"])

    def test_attempt_shown_as_missing(self):
        msgs = problem_prompt(self.problem, 1)
        self.assertIn("尚未提供", msgs[1]["content"])

    def test_attempt_shown_when_present(self):
        self.problem["my_attempt"] = "E = kQ/r^2"
        msgs = problem_prompt(self.problem, 1)
        self.assertIn("E = kQ/r^2", msgs[1]["content"])

    def test_error_type_injected(self):
        """C6：错因进入提示并给出针对性要求。"""
        self.problem["error_type"] = "careless"
        msgs = problem_prompt(self.problem, 1)
        self.assertIn("粗心笔误", msgs[1]["content"])
        self.assertIn("符号正负号", msgs[1]["content"])

    def test_unknown_error_type_ignored(self):
        self.problem["error_type"] = "乱七八糟"
        msgs = problem_prompt(self.problem, 1)
        self.assertNotIn("（该学生标记的错因", msgs[1]["content"])


class TestSubjectAwareness(unittest.TestCase):
    """v1 学科感知人格：physics 默认保留原措辞，chem/math/未知回落中性，不再一律物理化。"""

    def test_resolve_exact_keys(self):
        self.assertEqual(_resolve_subject("math"), "math")
        self.assertEqual(_resolve_subject("物理"), "physics")
        self.assertEqual(_resolve_subject("化学"), "chemistry")

    def test_resolve_from_topic_alias(self):
        self.assertEqual(_resolve_subject("", "电磁感应"), "physics")
        self.assertEqual(_resolve_subject("", "线性代数"), "math")
        self.assertEqual(_resolve_subject("", "有机化学"), "chemistry")

    def test_resolve_unknown_returns_empty(self):
        self.assertEqual(_resolve_subject("", "完全未知的主题"), "")
        self.assertEqual(_resolve_subject(""), "")

    def test_profile_physics_default(self):
        p = _subject_profile("")
        self.assertIn("物理助教", p["ta_zh"])

    def test_profile_math_not_physics(self):
        p = _subject_profile("math")
        self.assertIn("数学助教", p["ta_zh"])
        self.assertNotIn("物理助教", p["ta_zh"])

    def test_profile_chem_not_physics(self):
        p = _subject_profile("chemistry")
        self.assertIn("化学助教", p["ta_zh"])
        self.assertNotIn("物理助教", p["ta_zh"])

    def test_problem_prompt_math_uses_math_ta(self):
        prob = {"subject": "math", "course": "高等数学", "topic": "导数",
                "content": "求导数", "my_attempt": ""}
        msgs = problem_prompt(prob, 1)
        self.assertIn("数学助教", msgs[0]["content"])
        self.assertNotIn("物理助教", msgs[0]["content"])

    def test_problem_prompt_physics_explicit(self):
        prob = {"subject": "physics", "course": "电磁学", "topic": "高斯定理",
                "content": "求场", "my_attempt": ""}
        msgs = problem_prompt(prob, 1)
        self.assertIn("物理助教", msgs[0]["content"])

    def test_extract_tags_returns_dict_local_fallback(self):
        # 无 AI 时回落本地规则，仍应返回结构化结果（不依赖网络）
        res = extract_tags("二次函数", "求二次函数的顶点坐标", subject="math")
        self.assertIn("tags", res)
        self.assertIn("source", res)


class TestSettingsCache(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="cache_", dir=_TEST_TMP_DIR)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "cache_test.db"
        db.DB_PATH = config.DB_PATH  # db 模块按值绑定，需同步替换
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        try:
            cls._tmp.cleanup()
        except OSError:
            # 沙箱安全删除机制在无回收站时拒绝 rmtree，忽略（临时文件留在 tests/.tmp）
            pass
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db

    def test_cache_returns_dict(self):
        invalidate_settings_cache()
        s = get_cached_settings()
        self.assertIsInstance(s, dict)

    def test_cache_returns_same_object_within_ttl(self):
        invalidate_settings_cache()
        s1 = get_cached_settings()
        s2 = get_cached_settings()
        self.assertIs(s1, s2)

    def test_invalidate_clears_cache(self):
        s1 = get_cached_settings()
        invalidate_settings_cache()
        s2 = get_cached_settings()
        self.assertIsNot(s1, s2)


if __name__ == "__main__":
    unittest.main()
