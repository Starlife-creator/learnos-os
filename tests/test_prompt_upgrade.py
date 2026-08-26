"""提示词工程升级回归测试（P0 落地）：

1. rag.filter_relevant 相对分阈值过滤（M3-lite，零 AI 调用）
2. ai._with_cache_breakpoint 缓存断点变换（M1，门控开关默认关）
3. handler_problems._rag_context 注入升级：k=5、阈值过滤、Source 标签、反虚构指令
4. oral handoff 指令：连续薄弱轮次触发"先点破再追问"
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
import rag


_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)


class TestFilterRelevant(unittest.TestCase):
    """M3-lite：相对分阈值过滤。"""

    def test_keeps_above_ratio_drops_long_tail(self):
        hits = [{"score": 10.0}, {"score": 8.0}, {"score": 3.0}, {"score": 1.0}]
        out = rag.filter_relevant(hits, ratio=0.35)
        self.assertEqual([h["score"] for h in out], [10.0, 8.0])  # 3 < 3.5 被剔除

    def test_best_hit_always_kept_even_if_low_absolute(self):
        hits = [{"score": 0.5}, {"score": 0.2}]
        out = rag.filter_relevant(hits, ratio=0.35)
        self.assertEqual(len(out), 2)  # 0.2 >= 0.175，绝对分低但相对贴近 → 保留

    def test_empty_input(self):
        self.assertEqual(rag.filter_relevant([]), [])

    def test_all_zero_scores_keep_first_only(self):
        hits = [{"score": 0}, {"score": 0}, {"score": 0}]
        self.assertEqual(len(rag.filter_relevant(hits)), 1)


class TestCacheBreakpoint(unittest.TestCase):
    """M1：缓存断点变换（纯函数行为，不发起网络请求）。"""

    def test_transforms_first_system_message(self):
        from ai import _with_cache_breakpoint
        msgs = [{"role": "system", "content": "你是助教"},
                {"role": "user", "content": "hi"}]
        out = _with_cache_breakpoint(msgs)
        # 原入参不被修改
        self.assertEqual(msgs[0]["content"], "你是助教")
        # system 变 parts 形式并带 ephemeral 断点；user 不变
        part = out[0]["content"][0]
        self.assertEqual(part["type"], "text")
        self.assertEqual(part["cache_control"], {"type": "ephemeral"})
        self.assertEqual(out[1], {"role": "user", "content": "hi"})

    def test_no_system_message_passthrough(self):
        from ai import _with_cache_breakpoint
        msgs = [{"role": "user", "content": "q"}]
        out = _with_cache_breakpoint(msgs)
        self.assertEqual(out[0]["content"], "q")

    def test_flag_off_by_default_in_payload(self):
        """默认关闭：payload 中不得出现 cache_control。"""
        import ai
        with patch.object(ai, "get_cached_settings", return_value={
                "api_key": "k", "api_base": "https://api.example.com/v1",
                "model": "m", "prompt_cache_control": "0"}):
            _, _, payload, _, _ = ai._prepare_ai_request(
                [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
                max_tokens=256, tier=None, route="test", stream=False)
        self.assertNotIn("cache_control", payload.decode("utf-8"))

    def test_flag_on_injects_breakpoint(self):
        import json as _json
        import ai
        cfg = {"api_key": "k", "api_base": "https://api.example.com/v1",
               "model": "m", "prompt_cache_control": "1"}
        with patch.object(ai, "get_cached_settings", return_value=cfg):
            _, _, payload, _, _ = ai._prepare_ai_request(
                [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
                max_tokens=256, tier=None, route="test", stream=False)
        body = _json.loads(payload.decode("utf-8"))
        self.assertEqual(body["messages"][0]["content"][0]["cache_control"],
                         {"type": "ephemeral"})


class TestRagContextInjection(unittest.TestCase):
    """M2：_rag_context 升级——k=5、阈值过滤、Source 标签、反虚构指令。"""

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="ragctx_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls.temp_dir.name) / "ragctx.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db
        cls.temp_dir.cleanup()

    def _make_handler(self):
        from handler import Handler
        h = Handler.__new__(Handler)  # 绕过 socket 初始化，仅调 _rag_context
        return h

    def test_sources_labeled_and_antifabrication_present(self):
        h = self._make_handler()
        problem = {"topic": "牛顿第二定律", "title": "F=ma", "content": "质量与加速度关系"}
        fake_hits = [
            {"doc_id": 1, "chunk_id": 11, "page": 3, "score": 9.0,
             "content": "F=ma，力与加速度同向。"},
            {"doc_id": 1, "chunk_id": 12, "page": 4, "score": 1.0,
             "content": "沾边长尾片段。"},
        ]
        fake_docs = [{"id": 1, "source_path": "/tmp/教材.pdf"}]
        with patch("rag.search", return_value=fake_hits), \
             patch("rag.list_docs", return_value=fake_docs):
            messages, sources = h._rag_context(problem)
        self.assertEqual(len(sources), 1)  # 1.0 < 0.35*9 → 长尾被阈值剔除
        self.assertIn("[Source 1｜教材.pdf 第3页]", messages[0]["content"])
        self.assertIn("不得把片段中没有的内容说成出自教材", messages[0]["content"])
        self.assertIn("优先采用与上述片段一致的表述", messages[0]["content"])

    def test_no_hits_returns_empty(self):
        h = self._make_handler()
        with patch("rag.search", return_value=[]):
            messages, sources = h._rag_context({"topic": "x"})
        self.assertEqual(messages, [])
        self.assertEqual(sources, [])


class TestOralHandoff(unittest.TestCase):
    """M5：连续薄弱轮次触发 handoff 指令（不发起真实调用）。"""

    def _capture_instruction(self, level: int, turn: int) -> str:
        import oral
        captured = {}

        def fake_call(messages, **kwargs):
            captured["system"] = messages[0]["content"]
            return "ok"

        transcript = ([{"role": "assistant", "content": "q"}]
                      + [{"role": "user", "content": "答"} for _ in range(turn)])
        with patch.object(oral, "call_ai", side_effect=fake_call):
            oral._ai_followup(transcript, "牛顿第二定律", "concept", level, turn, "physics")
        return captured["system"]

    def test_weak_streak_triggers_handoff(self):
        text = self._capture_instruction(level=1, turn=3)
        self.assertIn("先给一句简短通俗的讲解", text)
        self.assertIn("验证性追问", text)

    def test_strong_answer_no_handoff(self):
        text = self._capture_instruction(level=3, turn=3)
        self.assertNotIn("先给一句简短通俗的讲解", text)

    def test_early_weak_turn_no_handoff(self):
        text = self._capture_instruction(level=1, turn=1)
        self.assertNotIn("先给一句简短通俗的讲解", text)


class TestMergeRelatedUnion(unittest.TestCase):
    """M4-a：同名概念跨批 related 并集合并。"""

    def test_duplicate_concept_related_unioned(self):
        import material
        acc = {"chapters": [{"name": "力学"}],
               "concepts": [{"name": "惯性", "chapter": "力学", "related": ["质量"]},
                            {"name": "牛顿第二定律", "chapter": "力学", "related": []}]}
        part = {"chapters": [], "concepts": [
            {"name": "惯性", "chapter": "", "related": ["质量", "受力分析", "惯性"]},
            {"name": "摩擦力", "chapter": "力学", "related": []},
        ]}
        material._merge_concepts(acc, part)
        inertia = next(c for c in acc["concepts"] if c["name"] == "惯性")
        self.assertEqual(inertia["related"], ["质量", "受力分析"])  # 并集+去自身+去重
        names = [c["name"] for c in acc["concepts"]]
        self.assertEqual(names.count("惯性"), 1)  # 不产生重复概念
        self.assertIn("摩擦力", names)

    def test_first_nonempty_chapter_kept(self):
        import material
        acc = {"chapters": [], "concepts": [{"name": "A", "chapter": "一", "related": []}]}
        part = {"chapters": [], "concepts": [{"name": "A", "chapter": "二", "related": []}]}
        material._merge_concepts(acc, part)
        self.assertEqual(acc["concepts"][0]["chapter"], "一")


class TestCompleteRelations(unittest.TestCase):
    """M4-b：跨片段建边第二遍——清单内配对、未知名/自连剔除。"""

    def _acc(self):
        return {"chapters": [{"name": "力学"}], "concepts": [
            {"name": "惯性", "chapter": "力学", "related": []},
            {"name": "质量", "chapter": "力学", "related": []},
        ]}

    def test_known_pairs_added_unknown_and_self_dropped(self):
        import material
        raw = ('{"concepts": [{"name": "惯性", "related": ["质量", "幽灵概念", "惯性"]},'
               ' {"name": "不存在", "related": ["质量"]}]}')
        with patch("ai.call_ai", return_value=raw):
            added = material.complete_relations(self._acc())
        self.assertEqual(added, 1)
        acc = self._acc()
        with patch("ai.call_ai", return_value=raw):
            material.complete_relations(acc)
        inertia = next(c for c in acc["concepts"] if c["name"] == "惯性")
        self.assertEqual(inertia["related"], ["质量"])

    def test_too_few_concepts_short_circuit(self):
        import material
        acc = {"chapters": [], "concepts": [{"name": "唯一", "chapter": "", "related": []}]}
        with patch("ai.call_ai") as mock_call:  # 不应发起调用
            self.assertEqual(material.complete_relations(acc), 0)
        mock_call.assert_not_called()

    def test_ai_failure_propagates_for_warning_path(self):
        import material
        with patch("ai.call_ai", side_effect=RuntimeError("离线")):
            with self.assertRaises(RuntimeError):
                material.complete_relations(self._acc())


class TestPromptInvariants(unittest.TestCase):
    """M7：提示词措辞不变量 + 版本号（改提示词必须同步更新并跑本测试）。"""

    ROOT = Path(__file__).resolve().parent.parent

    def _src(self, name: str) -> str:
        return (self.ROOT / name).read_text(encoding="utf-8")

    def test_prompt_version_format_and_exposed_in_metrics(self):
        import ai
        self.assertRegex(ai.PROMPT_VERSION, r"^\d{4}\.\d{2}$")
        handler_src = self._src("handler.py")
        self.assertIn("prompt_version", handler_src)

    def test_learn_qa_hard_grounding_wording(self):
        src = self._src("handler_learn.py")
        self.assertIn("教材未涉及", src)
        self.assertIn("不得调用训练知识猜测编造", src)

    def test_rag_context_antifabrication_wording(self):
        src = self._src("handler_problems.py")
        self.assertIn("不得把片段中没有的内容说成出自教材", src)
        self.assertIn("[Source {len(sources)}｜", src)

    def test_oral_handoff_wording(self):
        src = self._src("oral.py")
        self.assertIn("先给一句简短通俗的讲解", src)

    def test_rag_filter_relevant_exists(self):
        src = self._src("rag.py")
        self.assertIn("def filter_relevant", src)


class TestStructuredOutputGate(unittest.TestCase):
    """M6：结构化输出双闸门（调用点声明 json_mode + 用户开关 json_response_format）。"""

    _BASE = {"api_key": "k", "api_base": "https://api.example.com/v1", "model": "m"}

    def _payload(self, cfg_extra: dict, **kw) -> dict:
        import json as _json
        import ai
        cfg = dict(self._BASE)
        cfg.update(cfg_extra)
        with patch.object(ai, "get_cached_settings", return_value=cfg):
            _, _, payload, _, _ = ai._prepare_ai_request(
                [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
                max_tokens=256, tier=None, route="test", stream=False, **kw)
        return _json.loads(payload.decode("utf-8"))

    def test_switch_off_no_response_format(self):
        """开关默认关：即使调用点声明 json_mode 也不下发（零行为变更）。"""
        body = self._payload({"json_response_format": "0"}, json_mode=True)
        self.assertNotIn("response_format", body)

    def test_switch_on_but_not_json_call_site(self):
        """开关开、但调用点未声明 json_mode（纯文本产出）→ 不得下发。"""
        body = self._payload({"json_response_format": "1"}, json_mode=False)
        self.assertNotIn("response_format", body)

    def test_both_gates_open_injects(self):
        body = self._payload({"json_response_format": "1"}, json_mode=True)
        self.assertEqual(body["response_format"], {"type": "json_object"})

    def test_skip_preset_strips_response_format(self):
        """400 降级重试路径（skip_preset=True）必须剥离 response_format。"""
        body = self._payload({"json_response_format": "1"},
                             json_mode=True, skip_preset=True)
        self.assertNotIn("response_format", body)

    def test_400_retry_keywords_cover_response_format(self):
        src = (Path(__file__).resolve().parent.parent / "ai.py").read_text(encoding="utf-8")
        for kw in ("response_format", "json_object"):
            self.assertIn(f'"{kw}"', src)

    def test_json_call_sites_declared(self):
        """JSON 产出调用点必须声明 json_mode=True；纯文本调用点不得声明。"""
        root = Path(__file__).resolve().parent.parent
        ai_src = (root / "ai.py").read_text(encoding="utf-8")
        mat_src = (root / "material.py").read_text(encoding="utf-8")
        cards_src = (root / "cards.py").read_text(encoding="utf-8")
        # ai.py 6 处 + material.py 6 处 + cards.py 1 处
        self.assertGreaterEqual(ai_src.count("json_mode=True"), 6)
        self.assertGreaterEqual(mat_src.count("json_mode=True"), 6)
        self.assertGreaterEqual(cards_src.count("json_mode=True"), 1)
        # explain_concept 是纯文本详解，其调用行不得带 json_mode
        for line in ai_src.splitlines():
            if 'max_tokens=500, tier="heavy", route="material"' in line:
                self.assertNotIn("json_mode", line)


class TestSettingsSchemaReachable(unittest.TestCase):
    """M1/M6 开关必须注册进 SETTINGS_SCHEMA，否则 coerce_setting 抛「未知设置项」→ 开关不可达。"""

    def test_switches_registered(self):
        for key in ("prompt_cache_control", "json_response_format", "embedding_model"):
            self.assertIn(key, config.SETTINGS_SCHEMA, f"{key} 未注册 → 用户无法保存")

    def test_bool_switches_coerce(self):
        for key in ("prompt_cache_control", "json_response_format"):
            self.assertEqual(config.coerce_setting(key, "1"), "1")
            self.assertEqual(config.coerce_setting(key, ""), "0")
            self.assertEqual(config.coerce_setting(key, "false"), "0")

    def test_defaults_are_off(self):
        for key in ("prompt_cache_control", "json_response_format"):
            self.assertEqual(config.SETTINGS_SCHEMA[key]["default"], "0")

    def test_display_settings_exposes_switches(self):
        """设置页需能读回开关状态，否则 UI 每次加载都显示"关"。"""
        import ai
        with patch.object(ai, "get_cached_settings", return_value={
                "prompt_cache_control": "1", "json_response_format": "1"}):
            s = ai.display_settings()
        self.assertIs(s["prompt_cache_control"], True)
        self.assertIs(s["json_response_format"], True)

    def test_ui_wired_end_to_end(self):
        """HTML 控件 + JS 读取 + JS 保存三处齐全，缺一即开关不可用。"""
        static = Path(__file__).resolve().parent.parent / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        js = (static / "app-settings.js").read_text(encoding="utf-8")
        for el in ("setJsonResponseFormat", "setPromptCacheControl"):
            self.assertIn(f'id="{el}"', html, f"{el} 缺 HTML 控件")
            self.assertGreaterEqual(js.count(el), 2, f"{el} 需在读取与保存两处接线")
        for key in ("json_response_format", "prompt_cache_control"):
            self.assertIn(f"body.{key} =", js, f"{key} 未接入保存请求体")


if __name__ == "__main__":
    unittest.main()
