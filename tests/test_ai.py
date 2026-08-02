"""测试 AI 相关函数：URL 拼接、降级提示。"""
import tempfile
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ai import api_endpoint, fallback_hint, problem_prompt, get_cached_settings, invalidate_settings_cache
import config
import db


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


class TestSettingsCache(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix="cache_")
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmpdir) / "cache_test.db"
        db.init_db()

    @classmethod
    def tearDownClass(cls):
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
