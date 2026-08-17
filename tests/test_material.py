"""资料导入向导测试：AI 提取（mock）草稿流程、启发式降级、apply 写入、HTTP 端点。"""
import json
import sys
import tempfile
import unittest
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
import material
from handler import Handler

_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)

_MD = """# 力学

## 牛顿第一定律

惯性是物体的固有属性。

## 牛顿第二定律

F=ma，相关概念：牛顿第一定律。

例1 下列说法正确的是（ ）
A. 力是维持运动的原因
B. 惯性与速度有关
C. 质量是惯性大小的量度
D. 牛顿第二定律只适用于低速宏观
答案：C
解：质量是惯性大小的唯一量度。
"""


class TestMaterialUnit(unittest.TestCase):
    def test_batch_chars_adaptive(self):
        # 8k 上下文 → 4k 字符/批（下限）；32k+ → 12k（上限，防单批过大致输出截断）
        self.assertEqual(material.batch_chars(8000), 4000)
        self.assertEqual(material.batch_chars(32000), 12000)
        self.assertEqual(material.batch_chars(128000), 12000)
        self.assertEqual(material.batch_chars(1000000), 12000)

    def test_split_batches_full_coverage(self):
        size = 5000
        text = "a" * 10 + "\n\n" + "b" * size + "\n\n" + "c" * 10
        batches = material._split_batches(text, size)
        self.assertGreaterEqual(len(batches), 3)
        # 全文覆盖：每个段落都完整出现在某一批里
        joined = "\n\n".join(batches)
        self.assertIn("a" * 10, joined)
        self.assertIn("b" * size, joined)
        self.assertIn("c" * 10, joined)
        # 超长无空行大块被强制切片
        big = "x" * (size * 3)
        self.assertEqual(len(material._split_batches(big, size)), 3)

    def test_heuristic_concepts(self):
        draft = material._heuristic_concepts(_MD)
        names = [c["name"] for c in draft["concepts"]]
        self.assertEqual(draft["chapters"][0]["name"], "力学")
        self.assertIn("牛顿第一定律", names)
        self.assertIn("牛顿第二定律", names)
        self.assertEqual(draft["concepts"][1]["chapter"], "力学")

    def test_analyze_fallback_without_ai(self):
        # AI 未配置（call_ai 抛 ValueError）→ concepts 目标降级启发式
        with mock.patch("ai.call_ai", side_effect=ValueError("请先在「AI 设置」中填写 API 地址、密钥和模型。")):
            result = material.analyze(_MD, "physics", ["concepts"])
        self.assertEqual(result["source"], "heuristic")
        self.assertEqual(result["draft"]["concepts"]["chapters"][0]["name"], "力学")

    def test_analyze_fails_for_questions_without_ai(self):
        # 未配置 AI + questions 目标（无启发式兜底）→ 逐批失败不 raise，返回空 + warning
        with mock.patch("ai.call_ai", side_effect=ValueError("not configured")):
            result = material.analyze(_MD, "physics", ["questions"])
        self.assertEqual(result["source"], "ai")
        self.assertEqual(result["draft"]["questions"], [])
        self.assertTrue(result["warnings"])

    def test_analyze_ai_concepts_and_questions(self):
        ai_concepts = json.dumps({
            "chapters": [{"name": "力学"}],
            "concepts": [
                {"name": "牛顿第一定律", "chapter": "力学", "related": []},
                {"name": "牛顿第二定律", "chapter": "力学", "related": ["牛顿第一定律"]},
            ],
        })
        ai_questions = json.dumps({
            "questions": [{
                "stem": "下列说法正确的是", "choices": ["A", "B", "C", "D"], "answer": 2,
                "explain": "质量是惯性大小的量度", "concept": "惯性", "unit": "力学", "difficulty": 2,
            }],
        })
        with mock.patch("ai.call_ai", side_effect=[ai_concepts, ai_questions]):
            result = material.analyze(_MD, "physics", ["concepts", "questions"])
        self.assertEqual(result["source"], "ai")
        self.assertEqual(len(result["draft"]["concepts"]["concepts"]), 2)
        self.assertEqual(len(result["draft"]["questions"]), 1)

    def test_analyze_ai_full_coverage_by_context(self):
        # 200k 字符长文：32k 上下文 → >8 批全覆盖（旧实现只分析 8 批）
        calls = {"n": 0}

        def fake_call_ai(messages, **kwargs):
            i = calls["n"]
            calls["n"] += 1
            return json.dumps({
                "chapters": [{"name": f"第{i}章"}],
                "concepts": [{"name": f"概念{i}", "chapter": f"第{i}章", "related": []}],
            })

        long_text = "\n\n".join(f"第{i}章 内容 {('x' * 200)}" for i in range(1000))
        with mock.patch("ai.call_ai", side_effect=fake_call_ai):
            result = material.analyze(long_text, "physics", ["concepts"], context_tokens=32000)
        self.assertEqual(result["truncated"], False)
        self.assertGreater(result["batches"], 8)
        self.assertEqual(result["ai_calls"], result["batches"])
        self.assertEqual(len(result["draft"]["concepts"]["concepts"]), result["batches"])
        self.assertEqual(calls["n"], result["batches"])

    def test_analyze_bad_schema_falls_back(self):
        # AI 返回非 JSON → SchemaError → 单批失败（=全部批失败）→ concepts 回退启发式
        with mock.patch("ai.call_ai", return_value="not json"):
            result = material.analyze(_MD, "physics", ["concepts"])
        self.assertEqual(result["source"], "heuristic")
        self.assertTrue(result["warnings"])

    def test_analyze_all_batches_fail_falls_back(self):
        # 多批长文 + 全部批返回非 JSON → concepts 目标回退启发式给出产出
        long_text = "\n\n".join(f"# 第{i}章\n\n## 概念{i}\n\n内容" for i in range(3))
        with mock.patch("ai.call_ai", return_value="not json"):
            result = material.analyze(long_text, "physics", ["concepts"], context_tokens=2000)
        self.assertEqual(result["source"], "heuristic")
        self.assertTrue(result["draft"]["concepts"]["concepts"])

    def test_analyze_skip_failed_batch_keep_successes(self):
        # 前 2 批成功、第 3 批失败 → 保留成功批结果，不整体降级
        good = json.dumps({"chapters": [{"name": "力学"}],
                           "concepts": [{"name": "惯性", "chapter": "力学", "related": []}]})
        calls = {"n": 0}

        def flaky(messages, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                return ""  # 第 3 批失败（空返回 → JSON 解析 char 0）
            return good

        # 每章约 6KB 内容 → batch_chars(2000)=4000 → 3 批
        long_text = "\n\n".join(f"# 第{i}章\n\n## 概念{i}\n\n{('内容' * 3000)}" for i in range(3))
        with mock.patch("ai.call_ai", side_effect=flaky):
            result = material.analyze(long_text, "physics", ["concepts"], context_tokens=2000)
        self.assertEqual(result["source"], "ai")
        self.assertGreaterEqual(len(result["draft"]["concepts"]["concepts"]), 1)
        self.assertGreaterEqual(len(result["warnings"]), 1)
        # 失败批被跳过，但后续批继续调用（不是一失败就中断整体）
        self.assertGreaterEqual(calls["n"], 3)


class TestMaterialEndpoints(unittest.TestCase):
    server: ThreadingHTTPServer
    thread: Thread

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="mat_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "mat_test.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db

    def _request(self, method, path, body=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = json.dumps(body) if body is not None else None
        headers = {"Content-Type": "application/json", "X-Requested-With": "LearnOS"}
        conn.request(method, path, data, headers)
        resp = conn.getresponse()
        result = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, result

    def test_analyze_endpoint_needs_source(self):
        status, data = self._request("POST", "/api/material/analyze", {"targets": ["concepts"]})
        self.assertEqual(status, 400)

    def test_analyze_endpoint_heuristic(self):
        with mock.patch("ai.call_ai", side_effect=ValueError("not configured")):
            status, data = self._request("POST", "/api/material/analyze",
                                         {"targets": ["concepts"], "text": _MD})
        self.assertEqual(status, 200)
        self.assertEqual(data["source"], "heuristic")
        self.assertTrue(data["draft"]["concepts"]["concepts"])

    def test_apply_endpoint_writes_all(self):
        draft = {
            "concepts": {
                "chapters": [{"name": "力学"}],
                "concepts": [
                    {"name": "牛顿第一定律", "chapter": "力学", "related": []},
                    {"name": "牛顿第二定律", "chapter": "力学", "related": ["牛顿第一定律"]},
                ],
            },
            "questions": [{
                "stem": "质量是惯性大小的量度", "choices": ["对", "错"], "answer": 0,
                "explain": "", "concept": "惯性", "unit": "力学", "difficulty": 2,
            }],
            "paper": {"name": "力学测试卷", "questions": [
                {"qno": "1", "topic": "惯性", "content": "关于惯性的说法", "weight": 1},
            ]},
        }
        status, data = self._request("POST", "/api/material/apply", {"draft": draft})
        self.assertEqual(status, 200, data)
        stats = data["stats"]
        self.assertEqual(stats["concepts_added"], 3)  # 1 章 + 2 概念
        self.assertEqual(stats["questions_imported"], 1)
        self.assertEqual(stats["paper"]["added"], 1)
        # 关联边已建（牛顿第二定律 ↔ 牛顿第一定律）
        links = db.rows("SELECT concept_a, concept_b FROM concept_links WHERE relation = 'related'")
        self.assertEqual(len(links), 1)
        # 幂等：重复 apply 不产生重复概念，第二次新增计数为 0
        status, data2 = self._request("POST", "/api/material/apply", {"draft": draft})
        self.assertEqual(status, 200)
        self.assertEqual(data2["stats"]["concepts_added"], 0)
        total = db.rows("SELECT COUNT(*) AS c FROM concepts WHERE name IN ('力学','牛顿第一定律','牛顿第二定律')")
        self.assertEqual(total[0]["c"], 3)

    def test_apply_endpoint_empty(self):
        status, _ = self._request("POST", "/api/material/apply", {"draft": {}})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
