"""借鉴功能回归测试：复习上限/画像注入/全局搜索/别名+未链接提及/最优保持率。"""
import json
import re
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
from handler import Handler

_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)


class TestBorrowedFeatures(unittest.TestCase):
    server: ThreadingHTTPServer
    thread: Thread

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="feat_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "feat_test.db"
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

    def _create_problem(self, extra=None):
        body = {"title": "测试题", "content": "1+1=?", "course": "测试", "topic": "t",
                "error_type": "概念不清", "subject": "physics"}
        body.update(extra or {})
        status, data = self._request("POST", "/api/problems", body)
        self.assertIn(status, (200, 201), data)
        return data["id"]

    # ── 1. 复习队列：优先级 + 每日上限 ──
    def test_review_queue_cap(self):
        for i in range(3):
            self._create_problem({"title": f"上限题{i}"})
        status, data = self._request("GET", "/api/reviews")
        self.assertEqual(status, 200)
        self.assertEqual(data["cap"], 0)
        self.assertFalse(data["capped"])
        self.assertEqual(len(data["items"]), data["total"])
        self.assertGreaterEqual(data["total"], 3)
        # 设置上限 2 → 只显示 2 条，带 capped 标记，total 不变
        total_before = data["total"]
        self._request("PUT", "/api/settings", {"daily_review_cap": "2"})
        status, data = self._request("GET", "/api/reviews")
        self.assertEqual(len(data["items"]), 2)
        self.assertTrue(data["capped"])
        self.assertEqual(data["total"], total_before)
        self._request("PUT", "/api/settings", {"daily_review_cap": "0"})

    # ── 2. 口试画像注入 ──
    def test_oral_profile_injection(self):
        captured = []

        def fake_call_ai(messages, **kwargs):
            captured.append(messages)
            return "请解释惯性。"

        with mock.patch("oral.call_ai", side_effect=fake_call_ai):
            status, data = self._request("POST", "/api/oral/start", {"topic": "牛顿第一定律"})
        self.assertEqual(status, 200)
        self.assertTrue(captured)
        system_content = captured[0][0]["content"]
        self.assertIn("学习者档案", system_content)      # 画像快照已注入
        self.assertIn("薄弱知识点方向追问", system_content)  # 教学策略调整指令

    # ── 3. 全局搜索 ──
    def test_global_search(self):
        from urllib.parse import quote
        self._create_problem({"title": "动量守恒专练", "content": "碰撞问题"})
        status, data = self._request("GET", "/api/search?q=" + quote("动量守恒"))
        self.assertEqual(status, 200)
        titles = [p["title"] for p in data["problems"]]
        self.assertIn("动量守恒专练", titles)
        status, data = self._request("GET", "/api/search?q=" + quote("不存在的东西xyz"))
        self.assertEqual(data["problems"], [])

    # ── 4. 别名 + 未链接提及 ──
    def test_alias_and_unlinked_mentions(self):
        _, add = self._request("POST", "/api/graph/concepts",
                               {"name": "动量守恒定律", "aliases": "动量守恒"})
        cid = add["id"]
        # 别名已保存
        status, ok = self._request("PUT", f"/api/graph/concepts/{cid}", {"aliases": "动量守恒,守恒定律"})
        self.assertEqual(status, 200)
        # 未绑定概念的错题文本提及别名 → 出现在建议里
        pid = self._create_problem({"title": "碰撞", "content": "本题考查动量守恒的应用"})
        status, data = self._request("GET", "/api/graph/unlinked")
        hits = [m for m in data["items"] if m["problem_id"] == pid and m["concept_id"] == cid]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["matched"], "动量守恒")
        self.assertEqual(hits[0]["concept_name"], "动量守恒定律")
        # 一键绑定 → concept_ids 规范格式，建议消失
        status, ok = self._request("POST", "/api/graph/bind",
                                   {"problem_id": pid, "concept_id": cid})
        self.assertEqual(status, 200)
        stored = db.row("SELECT concept_ids FROM problems WHERE id = ?", (pid,))["concept_ids"]
        self.assertEqual(stored, f",{cid},")
        status, data = self._request("GET", "/api/graph/unlinked")
        self.assertFalse([m for m in data["items"] if m["problem_id"] == pid and m["concept_id"] == cid])

    # ── B4 回归：fsrs 副作用端点只走 POST（GET 无 CSRF 校验，可被跨站 <img> 触发）──
    def test_fsrs_side_effect_endpoints_post_only(self):
        get_patterns = [p for p, _m in Handler.GET_ROUTES]
        self.assertNotIn("/api/fsrs/train", get_patterns)
        self.assertNotIn("/api/fsrs/reset", get_patterns)
        post_patterns = [p for p, _m, _b in Handler.POST_ROUTES] if hasattr(Handler, "POST_ROUTES") else []
        self.assertIn("/api/fsrs/train", post_patterns)
        self.assertIn("/api/fsrs/reset", post_patterns)
        # 注：不做 GET 实请求断言——未命中 API 路由会落到静态文件服务返回 HTML，非 JSON

    # ── 概念详解回档端点：DELETE 清空用户覆盖层，落回种子基线 ──    def test_revert_explanation_endpoint(self):
        _, add = self._request("POST", "/api/graph/concepts",
                               {"name": "回档测试概念", "aliases": "", "subject": "physics"})
        cid = add["id"]
        # 写入用户覆盖层
        status, _ = self._request("PUT", f"/api/graph/concepts/{cid}",
                                  {"aliases": "", "explanation": "用户覆盖"})
        self.assertEqual(status, 200)
        # 专用回档端点：DELETE 清空 explanation_user（落回种子基线）
        status, data = self._request("DELETE", f"/api/graph/concepts/{cid}/explanation-override")
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))
        row = db.row("SELECT explanation_user, explanation FROM concepts WHERE id=?", (cid,))
        self.assertIsNone(row["explanation_user"], "回档后用户层应为空")
        # 再次 DELETE 幂等，仍返回 ok
        status, data = self._request("DELETE", f"/api/graph/concepts/{cid}/explanation-override")
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))

    # ── 温度空串修复：float('') 曾导致所有 AI 调用崩溃 ──
    def test_temperature_empty_string_safe(self):
        from ai import _safe_temperature
        self.assertEqual(_safe_temperature({"temperature": ""}), 0.3)
        self.assertEqual(_safe_temperature({}), 0.3)
        self.assertEqual(_safe_temperature({"temperature": "abc"}), 0.3)
        self.assertEqual(_safe_temperature({"temperature": "0.7"}), 0.7)
        self.assertEqual(_safe_temperature({"temperature": "99"}), 2.0)  # 夹紧
        # 保存空串 → 存库回退 0.3；display_settings 不再返回空
        self._request("PUT", "/api/settings", {"temperature": ""})
        status, s = self._request("GET", "/api/settings")
        self.assertEqual(s["temperature"], "0.3")

    # ── 上下文窗口设置 + 缓存命中遥测 ──
    def test_context_and_cache_settings(self):
        # 上下文设置：保存/读取/夹紧
        status, _ = self._request("PUT", "/api/settings", {"ai_context_tokens": "128000"})
        self.assertEqual(status, 200)
        status, s = self._request("GET", "/api/settings")
        self.assertEqual(s["ai_context_tokens"], 128000)
        self._request("PUT", "/api/settings", {"ai_context_tokens": "1"})  # 过小 → 下限
        _, s = self._request("GET", "/api/settings")
        self.assertEqual(s["ai_context_tokens"], 4000)
        # 自定义任意值（>200k 上限之外的档位）存取一致
        self._request("PUT", "/api/settings", {"ai_context_tokens": "262144"})
        _, s = self._request("GET", "/api/settings")
        self.assertEqual(s["ai_context_tokens"], 262144)
        # 空返回 → 可读错误（而非 "JSON 解析失败"）
        import ai
        with mock.patch("ai._prepare_ai_request", return_value=(
                "test-model", "https://example.test/v1/chat/completions",
                b"{}", {"Authorization": "Bearer x"}, 0.0)):
            with mock.patch("ai._post_json", return_value={
                    "choices": [{"message": {"content": ""}}], "usage": {}}):
                with self.assertRaises(RuntimeError) as ctx:
                    ai.call_ai([{"role": "user", "content": "hi"}], route="test")
        self.assertIn("空内容", str(ctx.exception))
        # 缓存 token 提取：DeepSeek / OpenAI 两种字段
        from ai import _cached_tokens
        self.assertEqual(_cached_tokens({"prompt_cache_hit_tokens": 120}), 120)
        self.assertEqual(_cached_tokens({"prompt_tokens_details": {"cached_tokens": 80}}), 80)
        self.assertEqual(_cached_tokens({}), 0)
        # 遥测记录 + 摘要含命中率
        import telemetry
        telemetry.record(route="test", model="m", ok=True, tokens=1000, cached=600)
        summary = telemetry.summary()
        self.assertIn("cached_tokens", summary)
        self.assertIn("cache_hit_rate", summary)
        # 出站目标校验：协议白名单 + 本地端点默认放行、可关
        from ai import _check_ai_target
        with self.assertRaises(ValueError):
            _check_ai_target("file:///etc/passwd")
        _check_ai_target("http://127.0.0.1:11434/v1")  # 默认允许（Ollama 特性）
        self._request("PUT", "/api/settings", {"allow_local_ai": "0"})
        with self.assertRaises(ValueError):
            _check_ai_target("http://127.0.0.1:11434/v1")
        self._request("PUT", "/api/settings", {"allow_local_ai": "1"})

    # ── 上传落盘端点：原始字节流 + 文件名校验 ──
    def test_material_upload_endpoint(self):
        from http.client import HTTPConnection as _HC
        conn = _HC("127.0.0.1", self.port, timeout=10)
        payload = b"# \xe7\xac\xac\xe4\xb8\x80\xe7\xab\xa0\n\n## \xe6\xa6\x82\xe5\xbf\xb5\n\n\xe5\x86\x85\xe5\xae\xb9"
        conn.request("POST", "/api/material/upload?name=test_upload.md", payload, {
            "Content-Type": "application/octet-stream",
            "X-Requested-With": "LearnOS",
            "Content-Length": str(len(payload)),
        })
        resp = conn.getresponse()
        result = json.loads(resp.read().decode("utf-8"))
        conn.close()
        self.assertEqual(resp.status, 201, result)
        self.assertTrue(result["path"].startswith("uploads/"))
        # 已落盘且内容一致
        from pathlib import Path as _P
        fp = _P(__file__).resolve().parent.parent / result["path"]
        self.assertTrue(fp.is_file())
        self.assertEqual(fp.read_bytes(), payload)
        # 测试自清理；沙箱 safe-delete 钩子可能拦截 unlink，忽略
        try:
            fp.unlink()
        except OSError:
            pass

    def test_material_upload_rejects_bad_name(self):
        from http.client import HTTPConnection as _HC
        conn = _HC("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/material/upload?name=..%2F..%2Fevil.md", b"x", {
            "Content-Type": "application/octet-stream",
            "X-Requested-With": "LearnOS",
            "Content-Length": "1",
        })
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        self.assertEqual(resp.status, 400)
        self.assertIn("不合法", data.get("error", ""))

    # ── 静态断言：HTML 引用的脚本文件必须存在（防引用被误删）──
    def test_html_script_refs_exist(self):
        root = Path(__file__).resolve().parent.parent
        for name in ("index.html", "concept_map.html"):
            html = (root / "static" / name).read_text(encoding="utf-8")
            refs = re.findall(r'<script(?: defer)? src="([^"]+)"', html)
            for ref in refs:
                rel = ref.lstrip("/")
                self.assertTrue((root / "static" / rel).is_file(),
                                f"{name} 引用的 {ref} 不存在")

    # ── 断点续跑：from_batch/max_batches 只跑指定窗口 ──
    def test_analyze_from_batch_window(self):
        import material
        calls = {"n": 0}

        def fake(messages, **kwargs):
            calls["n"] += 1
            i = calls["n"]
            return json.dumps({"chapters": [{"name": f"章{i}"}],
                               "concepts": [{"name": f"概{i}", "chapter": f"章{i}", "related": []}]})

        long_text = "\n\n".join(f"第{i}章 内容{'x'*500}" for i in range(400))  # ~20 万字符，>8 批
        with mock.patch("ai.call_ai", side_effect=fake):
            r1 = material.analyze(long_text, "physics", ["concepts"], context_tokens=32000,
                                  from_batch=0, max_batches=2)
        self.assertEqual(r1["from_batch"], 0)
        self.assertEqual(r1["to_batch"], 2)
        self.assertGreater(r1["batches_total"], 2)
        # 第二次窗口：from_batch=2 起，前一批不重跑
        calls["n"] = 0
        with mock.patch("ai.call_ai", side_effect=fake):
            r2 = material.analyze(long_text, "physics", ["concepts"], context_tokens=32000,
                                  from_batch=2, max_batches=2)
        self.assertEqual(r2["from_batch"], 2)
        self.assertEqual(r2["to_batch"], 4)
        self.assertEqual(calls["n"], 3)  # 窗口内 2 批 + M4 跨批建边第二遍 1 次

    # ── 版本号 ──
    def test_version_bump(self):
        status, data = self._request("GET", "/api/health")
        self.assertEqual(data["version"], "0.5.0")

    # ── 7. 最优保持率估算 ──
    def test_optimal_retention(self):
        import fsrs_bridge
        # 纯函数：稳定度 10 天、100 卡
        result = fsrs_bridge.optimal_retention([10.0] * 10, 100)
        self.assertTrue(result["has_data"])
        self.assertTrue(0.75 <= result["recommended"] <= 0.95)
        self.assertEqual(len(result["points"]), 5)  # 0.75..0.95 步长 0.05
        # 间隔随保持率升高而缩短（更频繁复习）
        intervals = [p["interval_days"] for p in result["points"]]
        self.assertEqual(intervals, sorted(intervals, reverse=True))
        # 端点：无数据 → assumed + has_data False
        empty = fsrs_bridge.optimal_retention([], 0)
        self.assertFalse(empty["has_data"])
        self.assertTrue(empty["assumed_stability"])
        # HTTP 端点
        status, data = self._request("GET", "/api/fsrs/optimal")
        self.assertEqual(status, 200)
        self.assertIn("recommended", data)
        self.assertIn("points", data)


if __name__ == "__main__":
    unittest.main()
