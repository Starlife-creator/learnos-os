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


class TestScoreHistory(unittest.TestCase):
    """评分历史落库/查询（需临时 DB，v22 迁移）。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="bank_score_hist_", dir=_TMP)
        cls._orig_config = config.DB_PATH
        cls._orig_db = db.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "test.db"
        db.DB_PATH = config.DB_PATH
        from db import init_db
        init_db()

    @classmethod
    def tearDownClass(cls):
        config.DB_PATH = cls._orig_config
        db.DB_PATH = cls._orig_db
        cls._tmp.cleanup()

    def test_migration_v22_table_exists(self):
        from db import rows
        cols = [r["name"] for r in rows("PRAGMA table_info(bank_scores)")]
        self.assertIn("qid", cols)
        self.assertIn("score", cols)

    def test_save_and_query_history(self):
        _force_offline()
        res = {"score": None, "ai_available": False, "mode": "unrated", "needs_review": True,
               "comment": "测试", "against": "要点"}
        pid = bank.save_score_history("physics-seed-subjective-3", "physics", res)
        self.assertGreater(pid, 0)
        hist = bank.recent_scores("physics-seed-subjective-3")
        self.assertEqual(len(hist), 1)
        self.assertIsNone(hist[0]["score"])
        self.assertTrue(hist[0]["needs_review"])
        self.assertEqual(hist[0]["comment"], "测试")


class TestCompositeShortStemImport(unittest.TestCase):
    """composite 子题短题干（如"（1）"）导入不应报"题干过短"，且不污染真实 custom 文件。"""

    def setUp(self):
        # 把 custom 题库指向临时文件，避免污染真实 bank_custom.json
        self._orig_custom = bank._custom_file
        self._tmpdir = tempfile.TemporaryDirectory(prefix="bank_imp_", dir=_TMP)
        tmp_path = Path(self._tmpdir.name)

        def _fake_custom(subject: str = "physics") -> Path:
            return tmp_path / f"custom_{subject}.json"

        bank._custom_file = _fake_custom

    def tearDown(self):
        bank._custom_file = self._orig_custom
        self._tmpdir.cleanup()
        bank._BANK.pop("physics", None)

    def test_import_composite_short_parts(self):
        items = [{
            "type": "composite", "stem": "解答下列小题：",
            "unit": "u", "chapter": "c", "concept": "k",
            "parts": [
                {"type": "single", "stem": "（1）", "choices": ["A", "B"], "answer": 1},
                {"type": "fill", "stem": "（2）", "answer": "0"},
            ],
        }]
        res = bank.import_questions(items, subject="physics")
        self.assertEqual(res["imported"], 1)
        self.assertEqual(res["errors"], [])


class TestAiConfigured(unittest.TestCase):
    """ai_configured 判断修正：文件存在 ≠ 已解锁可用。"""

    def tearDown(self):
        ai.set_runtime_key(None)
        ai.set_master_password(None)
        ai.invalidate_settings_cache()
        try:
            ai.ai_configured = ai.ai_configured  # 恢复（若被 mock 覆盖）
        except Exception:
            pass

    def _reset(self):
        ai.set_runtime_key(None)
        ai.set_master_password(None)
        ai.invalidate_settings_cache()

    def test_no_key_file_no_local_endpoint_false(self):
        # 无 runtime key、无 master password（且本地无 keys.enc 或未解锁）→ False
        self._reset()
        import keystore
        orig_exists = keystore.key_file_exists
        keystore.key_file_exists = lambda: False
        try:
            self.assertFalse(ai.ai_configured())
        finally:
            keystore.key_file_exists = orig_exists

    def test_runtime_key_true(self):
        self._reset()
        ai.set_runtime_key("sk-test-123")
        self.assertTrue(ai.ai_configured())

    def test_local_endpoint_allows_empty_key(self):
        # 本地 Ollama 端点允许空 key → 仍算可用
        self._reset()
        import ai as ai_mod
        orig = ai_mod.get_cached_settings
        ai_mod.get_cached_settings = lambda: {"api_base": "http://127.0.0.1:11434", "api_key": ""}
        try:
            self.assertTrue(ai.ai_configured())
        finally:
            ai_mod.get_cached_settings = orig

    def test_locked_keyfile_without_password_false(self):
        # keys.enc 存在但未解锁（无主口令）→ 旧实现误判 True，新实现应为 False
        self._reset()
        import keystore
        orig_exists = keystore.key_file_exists
        keystore.key_file_exists = lambda: True
        try:
            self.assertFalse(ai.ai_configured())
        finally:
            keystore.key_file_exists = orig_exists


class TestEnableThinking(unittest.TestCase):
    """非流式/DeepSeek 请求自动带 enable_thinking=false（reasoner 兼容）。"""

    def _prepare(self, **overrides):
        import json
        from unittest import mock
        cfg = {
            "api_base": "https://xxx.maas.aliyuncs.com/compatible-mode/v1",
            "api_key": "sk-test", "model": "deepseek-v4-flash-0731",
            "temperature": "0.3", "disable_thinking": "1",
        }
        cfg.update(overrides)
        with mock.patch.object(ai, "get_cached_settings", return_value=cfg):
            _, _, payload, _, _ = ai._prepare_ai_request(
                [{"role": "user", "content": "hi"}], 100, None, "test", stream=False)
        return json.loads(payload)

    def test_nonstream_deepseek_thinking_off(self):
        self.assertFalse(self._prepare().get("enable_thinking"))

    def test_nondeepseek_model_no_param(self):
        body = self._prepare(model="gpt-4o", api_base="https://api.openai.com/v1")
        self.assertIsNone(body.get("enable_thinking"))

    def test_stream_with_thinking_enabled_keeps(self):
        import json
        from unittest import mock
        cfg = {"api_base": "https://xxx.maas.aliyuncs.com/compatible-mode/v1",
               "api_key": "sk-test", "model": "deepseek-v4-flash-0731",
               "temperature": "0.3", "disable_thinking": "0"}
        with mock.patch.object(ai, "get_cached_settings", return_value=cfg):
            _, _, payload, _, _ = ai._prepare_ai_request(
                [{"role": "user", "content": "hi"}], 100, None, "test", stream=True)
        self.assertIsNone(json.loads(payload).get("enable_thinking"))

    def test_openai_reasoning_effort_none(self):
        # OpenAI o3/GPT-5：用 reasoning_effort=none 关闭思考
        body = self._prepare(model="o3-mini", api_base="https://api.openai.com/v1")
        self.assertEqual(body.get("reasoning_effort"), "none")

    def test_qwen_selfhosted_chat_template_kwargs(self):
        # Qwen 自托管/vLLM：chat_template_kwargs.enable_thinking=false
        body = self._prepare(model="Qwen/Qwen3.5-9B", api_base="http://127.0.0.1:8000/v1")
        self.assertEqual(body.get("chat_template_kwargs", {}).get("enable_thinking"), False)

    def test_qwen_bailian_enable_thinking(self):
        # Qwen 走阿里云百炼：用顶层 enable_thinking=false
        body = self._prepare(model="qwen-plus", api_base="https://xxx.maas.aliyuncs.com/compatible-mode/v1")
        self.assertFalse(body.get("enable_thinking"))

    def test_ollama_local_no_param(self):
        # Ollama 本地模型：不注入未知参数（防 400）
        body = self._prepare(model="llama3.1", api_base="http://127.0.0.1:11434")
        self.assertIsNone(body.get("enable_thinking"))
        self.assertIsNone(body.get("reasoning_effort"))
        self.assertIsNone(body.get("chat_template_kwargs"))

    def test_kimi_moonshot_thinking_disabled(self):
        # Kimi 官方（moonshot）：k2.6 用 thinking.type=disabled 关思考
        body = self._prepare(model="kimi-k2.6", api_base="https://api.moonshot.cn/v1")
        self.assertEqual(body.get("thinking", {}).get("type"), "disabled")

    def test_kimi_bailian_enable_thinking(self):
        # Kimi 走阿里云百炼：顶层 enable_thinking=false
        body = self._prepare(model="kimi-k2.6", api_base="https://xxx.maas.aliyuncs.com/compatible-mode/v1")
        self.assertFalse(body.get("enable_thinking"))

    def test_gemini_reasoning_effort_none(self):
        # Gemini OpenAI 兼容层：reasoning_effort=none
        body = self._prepare(model="gemini-2.5-pro", api_base="https://generativelanguage.googleapis.com/v1beta/openai/")
        self.assertEqual(body.get("reasoning_effort"), "none")

    def test_grok_reasoning_effort_none(self):
        body = self._prepare(model="grok-4", api_base="https://api.x.ai/v1")
        self.assertEqual(body.get("reasoning_effort"), "none")

    def test_claude_no_param(self):
        # Claude（OpenAI 兼容网关）：不注入（原生 thinking 参数不适用于兼容层）
        body = self._prepare(model="claude-opus-4-6", api_base="https://api.anthropic.com/v1")
        self.assertIsNone(body.get("thinking"))
        self.assertIsNone(body.get("reasoning_effort"))
        self.assertIsNone(body.get("enable_thinking"))

    def test_glm_zhipu_thinking_disabled(self):
        # GLM 智谱：thinking.type=disabled（GLM-4.5+ 官方）
        body = self._prepare(model="glm-4.5", api_base="https://open.bigmodel.cn/api/paas/v4")
        self.assertEqual(body.get("thinking", {}).get("type"), "disabled")

    def test_doubao_volces_thinking_disabled(self):
        # 豆包/火山方舟：thinking.type=disabled
        body = self._prepare(model="doubao-seed-1-6", api_base="https://ark.cn-beijing.volces.com/api/v3")
        self.assertEqual(body.get("thinking", {}).get("type"), "disabled")

    def test_hunyuan_thinking_disabled(self):
        body = self._prepare(model="hunyuan-turbo", api_base="https://api.hunyuan.cloud.tencent.com/v1")
        self.assertEqual(body.get("thinking", {}).get("type"), "disabled")

    def test_minimax_reasoning_split(self):
        # MiniMax M2+ 恒思考不能关 → reasoning_split=true 分离思考 token
        body = self._prepare(model="MiniMax-M2", api_base="https://api.minimaxi.com/v1")
        self.assertTrue(body.get("reasoning_split"))


if __name__ == "__main__":
    unittest.main()