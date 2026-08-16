"""测试 HTTP 端点的基本 CRUD 路径。"""
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
import keystore
from handler import Handler

# 测试临时数据严格限制在工作区内（tests/.tmp/），不留任何外部痕迹
_TEST_TMP_DIR = Path(__file__).resolve().parent / ".tmp"
_TEST_TMP_DIR.mkdir(exist_ok=True)


class TestEndpoints(unittest.TestCase):
    server: ThreadingHTTPServer
    thread: Thread

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="handler_", dir=_TEST_TMP_DIR)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "handler_test.db"
        db.DB_PATH = config.DB_PATH  # db 模块按值绑定，需同步替换
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
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = json.dumps(body) if body else None
        headers = {"Content-Type": "application/json", "X-Requested-With": "LearnOS"}
        conn.request(method, path, data, headers)
        resp = conn.getresponse()
        result = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, result

    def test_health(self):
        status, data = self._request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_hint_level4_fallback_and_diagnose(self):
        """A6：四级提示可生成（无 AI 走降级）；最近一次复习失败时返回诊断门建议。"""
        pid = self._create_problem()
        status, data = self._request("POST", f"/api/problems/{pid}/hint", {"level": 4})
        self.assertEqual(status, 200)
        self.assertTrue(data["content"])
        self.assertEqual(data["source"], "fallback")
        self.assertFalse(data["diagnose"])
        # 完成一次失败复习 → 再取提示应带诊断建议
        _, _rv = self._request("GET", "/api/reviews"); reviews = _rv["items"]
        rid = next(r["id"] for r in reviews if r["problem_id"] == pid)
        self._request("POST", f"/api/reviews/{rid}/complete", {"rating": 1})
        status, data = self._request("POST", f"/api/problems/{pid}/hint", {"level": 1})
        self.assertEqual(status, 200)
        self.assertTrue(data["diagnose"])

    def test_analytics(self):
        """D4：仪表盘扩展返回 7 天压力序列与卡组健康度。"""
        self._create_problem()
        status, data = self._request("GET", "/api/analytics")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["due_7d"]), 7)
        self.assertIn("deck_health", data)
        self.assertIn("newborn", data["deck_health"])
        self.assertIn("daily_reviews", data)

    def test_variants_generate_save_and_quality(self):
        """A4：变式生成（无 AI 走离线模板）→ 确认保存（R3）→ 复习后质量分回写。"""
        pid = self._create_problem()
        status, data = self._request("POST", f"/api/problems/{pid}/variants/generate", {})
        self.assertEqual(status, 200)
        self.assertIn("variants", data)
        self.assertGreaterEqual(len(data["variants"]), 1)
        v = data["variants"][0]
        self.assertIn("mode", v)
        self.assertTrue(v["title"] and v["content"])
        # 未确认不落库
        p = db.row("SELECT variants FROM problems WHERE id = ?", (pid,))
        self.assertEqual(json.loads(p["variants"]), [])
        # 确认保存
        status, data = self._request("POST", f"/api/problems/{pid}/variants", {"variants": data["variants"]})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(data["total"], 1)
        # 复习变式（variant_id=1）完成 → 质量分回写
        _, _rv = self._request("GET", "/api/reviews"); reviews = _rv["items"]
        rid = next(r["id"] for r in reviews if r["problem_id"] == pid)
        with db.db() as conn:
            conn.execute("UPDATE reviews SET variant_id = 1 WHERE id = ?", (rid,))
        self._request("POST", f"/api/reviews/{rid}/complete", {"rating": 4})
        p = db.row("SELECT variants FROM problems WHERE id = ?", (pid,))
        saved = json.loads(p["variants"])
        self.assertEqual(saved[0]["correct"], 1)
        self.assertEqual(saved[0]["total"], 1)
        # 详情返回解析后的数组
        status, got = self._request("GET", f"/api/problems/{pid}")
        self.assertEqual(status, 200)
        self.assertIsInstance(got["variants"], list)
        self.assertGreaterEqual(len(got["variants"]), 1)

    def test_profile_aggregate_and_goal(self):
        """C5：画像聚合返回结构；目标可写可读（仅本地）。"""
        self._create_problem()
        status, data = self._request("GET", "/api/profile")
        self.assertEqual(status, 200)
        self.assertIn("topics", data)
        self.assertIn("errors", data)
        self.assertIn("pace", data)
        self.assertIn("goal", data)
        self.assertIn("preferences", data)
        status, _ = self._request("PUT", "/api/profile", {"exam_date": "2026-12-20", "exam_target_score": "85"})
        self.assertEqual(status, 200)
        status, data = self._request("GET", "/api/profile")
        self.assertEqual(status, 200)
        self.assertEqual(data["goal"]["exam_date"], "2026-12-20")
        self.assertEqual(data["goal"]["exam_target_score"], "85")
        from profile import snapshot
        snap = snapshot()
        self.assertIn("2026-12-20", snap)
        self.assertIn("错因分布", snap)

    def test_models_probe(self):
        """C3：探测端点恒返回结构；未装 Ollama 时 available=false 或 null，不报错。"""
        status, data = self._request("GET", "/api/models/probe")
        self.assertEqual(status, 200)
        self.assertIn("ollama", data)
        if data["ollama"] is not None:
            self.assertIn("available", data["ollama"])
            self.assertIn("models", data["ollama"])

    def test_dashboard_empty(self):
        status, data = self._request("GET", "/api/dashboard")
        self.assertEqual(status, 200)
        self.assertIn("stats", data)
        self.assertIn("due", data)

    def _create_problem(self, title="端到端测试题", **overrides) -> int:
        body = {
            "title": title,
            "course": "力学",
            "topic": "能量守恒",
            "content": "求小球滑下斜面后的速度",
        }
        body.update(overrides)
        status, data = self._request("POST", "/api/problems", body)
        self.assertEqual(status, 201)
        self.assertIn("id", data)
        return data["id"]

    def test_create_problem(self):
        pid = self._create_problem()
        self.assertGreater(pid, 0)

    def test_create_validation(self):
        status, data = self._request("POST", "/api/problems", {"title": "", "content": ""})
        self.assertEqual(status, 400)

    def test_get_problem(self):
        pid = self._create_problem()
        status, data = self._request("GET", f"/api/problems/{pid}")
        self.assertEqual(status, 200)
        self.assertEqual(data["title"], "端到端测试题")
        self.assertIn("hints", data)

    def test_get_problem_not_found(self):
        status, data = self._request("GET", "/api/problems/99999")
        self.assertEqual(status, 404)

    def test_update_problem(self):
        pid = self._create_problem()
        status, data = self._request("PUT", f"/api/problems/{pid}", {"mastery": 4})
        self.assertEqual(status, 200)
        status, data = self._request("GET", f"/api/problems/{pid}")
        self.assertEqual(data["mastery"], 4)

    def test_extract_tags_local_fallback(self):
        """B5：无 AI 配置时自动降级关键词规则，返回合法标签草稿。"""
        status, data = self._request("POST", "/api/ai/extract-tags", {
            "title": "牛顿第二定律应用",
            "content": "求物体沿斜面下滑的加速度，分析受力并计算摩擦力",
            "course": "力学",
            "topic": "牛顿定律",
        })
        self.assertEqual(status, 200)
        self.assertIn("tags", data)
        self.assertIn("confidence", data)
        self.assertEqual(data["source"], "local")
        self.assertTrue(all(isinstance(t, str) and t for t in data["tags"]))

    def test_extract_tags_requires_content(self):
        status, data = self._request("POST", "/api/ai/extract-tags", {"title": "", "content": ""})
        self.assertEqual(status, 400)

    def test_tags_confirm_flow(self):
        """B5（R3）：标签草稿 → PUT 确认后才落库；未确认不写入。"""
        pid = self._create_problem()
        p = db.row("SELECT tags, tags_status FROM problems WHERE id = ?", (pid,))
        self.assertEqual(p["tags"], "[]")
        self.assertEqual(p["tags_status"], "none")
        status, _ = self._request("PUT", f"/api/problems/{pid}", {"tags": ["知识点:力学", "题型:计算题"]})
        self.assertEqual(status, 200)
        p = db.row("SELECT tags, tags_status FROM problems WHERE id = ?", (pid,))
        self.assertEqual(json.loads(p["tags"]), ["知识点:力学", "题型:计算题"])
        self.assertEqual(p["tags_status"], "confirmed")
        status, got = self._request("GET", f"/api/problems/{pid}")
        self.assertEqual(status, 200)
        self.assertEqual(got["tags"], ["知识点:力学", "题型:计算题"])

    def test_export_roundtrip_preserves_tags(self):
        pid = self._create_problem()
        self._request("PUT", f"/api/problems/{pid}", {"tags": ["知识点:力学"]})
        status, exported = self._request("GET", "/api/export")
        self.assertEqual(status, 200)
        src = next(p for p in exported["problems"] if p["id"] == pid)
        self.assertEqual(src["tags"], ["知识点:力学"])
        status, result = self._request("POST", "/api/import", exported)
        self.assertEqual(status, 200)
        restored = db.row("SELECT tags, tags_status FROM problems WHERE id = ?", (pid,))
        self.assertEqual(json.loads(restored["tags"]), ["知识点:力学"])
        self.assertEqual(restored["tags_status"], "confirmed")

    def test_delete_problem(self):
        pid = self._create_problem()
        status, data = self._request("DELETE", f"/api/problems/{pid}")
        self.assertEqual(status, 200)
        status, data = self._request("GET", f"/api/problems/{pid}")
        self.assertEqual(status, 404)

    def test_delete_not_found(self):
        status, data = self._request("DELETE", "/api/problems/99999")
        self.assertEqual(status, 404)

    def test_problems_pagination(self):
        # Create 3 problems
        for i in range(3):
            self._request("POST", "/api/problems", {"title": f"分页测试{i}", "content": "test"})
        # Request page 1 with limit 2
        status, data = self._request("GET", "/api/problems?page=1&limit=2")
        self.assertEqual(status, 200)
        self.assertIn("items", data)
        self.assertEqual(len(data["items"]), 2)
        self.assertGreaterEqual(data["total"], 3)
        self.assertGreaterEqual(data["pages"], 2)

    def test_reviews_empty(self):
        status, data = self._request("GET", "/api/reviews")
        self.assertEqual(status, 200)
        self.assertIsInstance(data["items"], list)
        self.assertIn("cap", data)

    def test_settings_get(self):
        status, data = self._request("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertIn("api_base", data)

    def test_settings_update(self):
        status, data = self._request("PUT", "/api/settings", {"model": "gpt-4o-mini"})
        self.assertEqual(status, 200)
        status, data = self._request("GET", "/api/settings")
        self.assertEqual(data["model"], "gpt-4o-mini")

    def test_oral_start_validation(self):
        status, data = self._request("POST", "/api/oral/start", {"topic": ""})
        self.assertEqual(status, 400)

    def test_static_index(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertIn("学习 OS", body)

    def test_pwa_assets(self):
        """C7：PWA 离线缓存资产（sw.js/manifest/图标）可访问。"""
        for path in ("/sw.js", "/manifest.json", "/icon-192.png", "/icon-512.png"):
            with self.subTest(path=path):
                conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
                conn.request("GET", path)
                resp = conn.getresponse()
                body = resp.read()
                conn.close()
                self.assertEqual(resp.status, 200)
                self.assertTrue(body)
        # sw.js 为网络优先策略，含 CACHE_NAME 与 fetch 处理
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/sw.js")
        resp = conn.getresponse()
        sw = resp.read().decode("utf-8")
        conn.close()
        self.assertIn("CACHE_NAME", sw)
        self.assertIn("fetch(event.request)", sw)

    def test_oral_end(self):
        # Start an oral session
        status, data = self._request("POST", "/api/oral/start", {"topic": "电磁感应"})
        self.assertEqual(status, 200)
        session_id = data["session_id"]
        # End it
        status, data = self._request("POST", f"/api/oral/{session_id}/end", {})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_oral_end_not_found(self):
        status, data = self._request("POST", "/api/oral/99999/end", {})
        self.assertEqual(status, 404)

    def test_csrf_blocks_post_without_header(self):
        # 缺少 X-Requested-With 的写请求应被 403 拦截
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/problems",
                     json.dumps({"title": "x", "content": "y"}).encode(),
                     {"Content-Type": "application/json"})
        resp = conn.getresponse()
        conn.close()
        self.assertEqual(resp.status, 403)

    def test_trend_records_after_review(self):
        pid = self._create_problem()
        # 完成一次复习，触发掌握度日志
        _, _rv = self._request("GET", "/api/reviews"); reviews = _rv["items"]
        rid = next(r["id"] for r in reviews if r["problem_id"] == pid)
        self._request("POST", f"/api/reviews/{rid}/complete", {"rating": 3})
        status, data = self._request("GET", "/api/trend")
        self.assertEqual(status, 200)
        self.assertIn("points", data)
        self.assertIn("summary", data)
        self.assertTrue(any("avg_mastery" in row for row in data["points"]))

    def test_complete_review_persists_fsrs_state(self):
        """A1：复习完成后 FSRS 状态列应被写入（vendored 可用时）。"""
        import fsrs_bridge
        if not fsrs_bridge.fsrs_available():
            self.skipTest("FSRS vendored 缺失")
        pid = self._create_problem()
        _, _rv = self._request("GET", "/api/reviews"); reviews = _rv["items"]
        rid = next(r["id"] for r in reviews if r["problem_id"] == pid)
        status, result = self._request("POST", f"/api/reviews/{rid}/complete", {"rating": 4})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(result["interval_days"], 1)
        p = db.row("SELECT state, stability, difficulty FROM problems WHERE id = ?", (pid,))
        self.assertEqual(p["state"], 2)  # Review
        self.assertGreater(p["stability"], 0)
        self.assertGreaterEqual(p["difficulty"], 0)

    def test_dashboard_merged_fields(self):
        """C6：/api/dashboard 单请求合并趋势/分析/错因分布。"""
        self._create_problem(title="C6合并题", error_type="careless")
        status, d = self._request("GET", "/api/dashboard")
        self.assertEqual(status, 200)
        for key in ("points", "summary", "due_7d", "deck_health", "daily_reviews", "error_distribution"):
            self.assertIn(key, d)
        careless = [e for e in d["error_distribution"] if e["type"] == "careless"]
        self.assertTrue(careless)
        self.assertGreaterEqual(careless[0]["count"], 1)
        self.assertIn("label", careless[0])

    def test_dashboard_error_trend(self):
        """C7：dashboard 含 error_trend（近30天 vs 历史占比 + delta）。"""
        from datetime import date, timedelta
        pid = self._create_problem(title="C7趋势题", error_type="careless")
        old = (date.today() - timedelta(days=60)).isoformat()
        with db.DB_LOCK, db.db() as conn:
            conn.execute("UPDATE problems SET created_at = ? WHERE id = ?", (old, pid))
        self._create_problem(title="C7趋势题2", error_type="concept_misunderstood")
        status, d = self._request("GET", "/api/dashboard")
        self.assertEqual(status, 200)
        trend = d["error_trend"]
        by_type = {t["type"]: t for t in trend}
        self.assertIn("careless", by_type)
        self.assertIn("concept_misunderstood", by_type)
        entry = by_type["careless"]
        for key in ("recent_count", "recent_pct", "total_pct", "delta", "label"):
            self.assertIn(key, entry)
        # 旧题不计入近30天
        self.assertEqual(entry["recent_count"], 0)
        # 新题计入近30天（套件内其他近期题共存，占比放松断言）
        self.assertGreaterEqual(by_type["concept_misunderstood"]["recent_count"], 1)
        self.assertGreater(by_type["concept_misunderstood"]["recent_pct"], 0.0)
        self.assertLessEqual(by_type["concept_misunderstood"]["delta"], 50.0)

    def test_duplicates_endpoint(self):
        """C7：相似题查重端点（bigram Jaccard + topic 加权）。"""
        content_a = "一个质量为 m 的物体在光滑水平面上，受恒力 F 作用，求其加速度。"
        pid = self._create_problem(title="查重题", topic="力学", content=content_a)
        self._create_problem(title="无关题", topic="电磁学",
                             content="电荷 q 在匀强磁场 B 中做匀速圆周运动，求轨道半径。")
        q_topic = urllib.parse.quote("力学")
        qs = urllib.parse.quote(content_a)
        status, r = self._request("GET", f"/api/problems/duplicates?content={qs}&topic={q_topic}&exclude={pid}")
        self.assertEqual(status, 200)
        self.assertIn("duplicates", r)
        self.assertFalse(r["duplicates"])  # 排除自身后无近似
        # 建近亲题（同内容），近似变体 → 命中近亲而非自身
        twin = self._create_problem(title="近亲题", topic="力学", content=content_a)
        variant = urllib.parse.quote("一个质量为 m 的物体在光滑水平面上，受恒力 F 的作用，求加速度大小。")
        status, r2 = self._request("GET", f"/api/problems/duplicates?content={variant}&topic={q_topic}&exclude={pid}")
        self.assertEqual(status, 200)
        self.assertTrue(r2["duplicates"])
        self.assertEqual(r2["duplicates"][0]["id"], twin)
        self.assertGreaterEqual(r2["duplicates"][0]["similarity"], 0.35)
        # 空内容 → 空结果
        status, r3 = self._request("GET", "/api/problems/duplicates?content=")
        self.assertEqual(status, 200)
        self.assertEqual(r3["duplicates"], [])
        # 空内容 → 空结果
        status, r3 = self._request("GET", "/api/problems/duplicates?content=")
        self.assertEqual(status, 200)
        self.assertEqual(r3["duplicates"], [])

    def test_similarity_unit(self):
        """C7：_similarity bigram Jaccard 单元。"""
        from handler import Handler
        self.assertGreater(Handler._similarity("完全相同内容abc", "完全相同内容abc"), 0.99)
        self.assertLess(Handler._similarity("天南地北毫无关系", "甲乙丙丁无关紧要"), 0.3)
        self.assertEqual(Handler._similarity("", "abc"), 0.0)

    def test_overdue_review_halved_interval(self):
        """C6：逾期 5-20 天复习，间隔减半重排。"""
        from datetime import date, timedelta
        from fsrs_bridge import next_interval_days
        pid = self._create_problem()
        _, _rv = self._request("GET", "/api/reviews"); reviews = _rv["items"]
        rid = next(r["id"] for r in reviews if r["problem_id"] == pid)
        with db.DB_LOCK, db.db() as conn:
            conn.execute("UPDATE reviews SET due_date = ?, interval_days = 16 WHERE id = ?",
                         ((date.today() - timedelta(days=16)).isoformat(), rid))
        status, result = self._request("POST", f"/api/reviews/{rid}/complete", {"rating": 3})
        self.assertEqual(status, 200)
        expected = max(1, next_interval_days(3, 16) // 2)
        self.assertEqual(result["interval_days"], expected)
        self.assertLessEqual(result["interval_days"], 16)

    def test_overdue_review_reset_after_21_days(self):
        """C6：逾期 >=21 天 → 降级为新学：interval=1、repetition=0、掌握度减一。"""
        from datetime import date, timedelta
        pid = self._create_problem()
        _, _rv = self._request("GET", "/api/reviews"); reviews = _rv["items"]
        rid = next(r["id"] for r in reviews if r["problem_id"] == pid)
        with db.DB_LOCK, db.db() as conn:
            conn.execute("UPDATE reviews SET due_date = ?, interval_days = 30 WHERE id = ?",
                         ((date.today() - timedelta(days=22)).isoformat(), rid))
            conn.execute("UPDATE problems SET repetition = 4, mastery = 3 WHERE id = ?", (pid,))
        status, result = self._request("POST", f"/api/reviews/{rid}/complete", {"rating": 3})
        self.assertEqual(status, 200)
        self.assertEqual(result["interval_days"], 1)
        p = db.row("SELECT repetition, mastery FROM problems WHERE id = ?", (pid,))
        self.assertEqual(p["repetition"], 0)
        self.assertEqual(p["mastery"], 4)  # 5 - 1 降级

    def test_export_import_roundtrip(self):
        self._create_problem()
        status, exported = self._request("GET", "/api/export")
        self.assertEqual(status, 200)
        self.assertIn("problems", exported)
        # 导入导出数据
        status, result = self._request("POST", "/api/import", exported)
        self.assertEqual(status, 200)
        self.assertIn("imported", result)
        self.assertIn("backup", result)

    def _raw_get(self, path, accept=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"X-Requested-With": "LearnOS"}
        if accept:
            headers["Accept"] = accept
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, resp.getheader("Content-Type", ""), body

    def test_export_anki_csv(self):
        self._create_problem(title="CSV导出题")
        status, ctype, body = self._raw_get("/api/export?format=anki-csv")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", ctype)
        text = body.decode("utf-8")
        self.assertTrue(text.startswith("\ufeff"))
        self.assertIn("CSV导出题", text)

    def test_export_ics(self):
        self._create_problem(title="ICS导出题")
        status, ctype, body = self._raw_get("/api/export?format=ics")
        self.assertEqual(status, 200)
        self.assertIn("text/calendar", ctype)
        text = body.decode("utf-8")
        self.assertIn("BEGIN:VCALENDAR", text)
        self.assertIn("END:VCALENDAR", text)
        self.assertIn("复习：ICS导出题", text)

    def test_create_problem_legacy_error_type_merged(self):
        pid = self._create_problem(error_type="概念不清")
        status, data = self._request("GET", f"/api/problems/{pid}")
        self.assertEqual(status, 200)
        self.assertEqual(data["error_type"], "concept_misunderstood")

    def test_create_problem_structured_fields(self):
        body = {
            "title": "结构化错因题", "content": "题干",
            "error_type": "calculation", "error_path": "第三行积分错误",
            "trap_note": "忽略奇偶性", "shortcut": "先对称性", "fix_action": "重算",
        }
        status, data = self._request("POST", "/api/problems", body)
        self.assertEqual(status, 201)
        pid = data["id"]
        _, got = self._request("GET", f"/api/problems/{pid}")
        self.assertEqual(got["error_path"], "第三行积分错误")
        self.assertEqual(got["shortcut"], "先对称性")

    def test_update_problem_error_type_normalized(self):
        pid = self._create_problem(error_type="马虎")
        status, data = self._request("PUT", f"/api/problems/{pid}", {"error_type": "粗心"})
        self.assertEqual(status, 200)
        _, got = self._request("GET", f"/api/problems/{pid}")
        self.assertEqual(got["error_type"], "careless")

    def test_batch_star_and_delete(self):
        """批量操作：star/delete 正常，非法 action 不报错。"""
        p1 = self._create_problem(title="批量1")
        p2 = self._create_problem(title="批量2")
        status, _ = self._request("POST", "/api/problems/batch", {"ids": [p1, p2], "action": "star"})
        self.assertEqual(status, 200)
        _, got = self._request("GET", f"/api/problems/{p1}")
        self.assertEqual(got["starred"], 1)
        _, got2 = self._request("GET", f"/api/problems/{p2}")
        self.assertEqual(got2["starred"], 1)
        status, _ = self._request("POST", "/api/problems/batch", {"ids": [p1], "action": "delete"})
        self.assertEqual(status, 200)
        status, _ = self._request("GET", f"/api/problems/{p1}")
        self.assertEqual(status, 404)

    def test_reviews_interleave_mode(self):
        # 清空共享测试库，避免历史测试遗留的 due 卡破坏分桶假设
        with db.db() as conn:
            conn.execute("DELETE FROM problems")
        for i, topic in enumerate(["力学", "电磁学", "热学"]):
            self._create_problem(title=f"交错题{i}", topic=topic)
        status, data = self._request("GET", "/api/reviews?mode=interleave")
        self.assertEqual(status, 200)
        topics = [r["topic"] for r in data["items"]]
        # 交错：相邻两题不应同知识点（分桶轮转）
        for a, b in zip(topics, topics[1:]):
            self.assertNotEqual(a, b)

    def test_today_summary(self):
        self._create_problem()
        _, _rv = self._request("GET", "/api/reviews"); reviews = _rv["items"]
        rid = reviews[0]["id"]
        self._request("POST", f"/api/reviews/{rid}/complete", {"rating": 4})
        status, data = self._request("GET", "/api/reviews/summary/today")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(data["done"], 1)
        self.assertIn("hard", data)
        self.assertIn("top_error", data)
        self.assertIn("accuracy", data)
        self.assertIsInstance(data["error_counts"], dict)

    def test_stubborn_problems(self):
        """P0：重复出错(≥2 次)的题进入顽固错题榜，含再错统计。"""
        pid = self._create_problem(title="P0顽固题", error_type="careless")
        for _ in range(2):  # 两次都答错
            _, _rv = self._request("GET", "/api/reviews"); reviews = _rv["items"]
            rid = next(r["id"] for r in reviews if r["problem_id"] == pid)
            status, _ = self._request("POST", f"/api/reviews/{rid}/complete", {"rating": 1})
            self.assertEqual(status, 200)
        status, d = self._request("GET", "/api/dashboard")
        self.assertEqual(status, 200)
        self.assertIn("stubborn", d)
        entry = next((s for s in d["stubborn"] if s["id"] == pid), None)
        self.assertIsNotNone(entry)
        self.assertGreaterEqual(entry["miss_count"], 2)
        self.assertEqual(entry["total_reviews"], entry["miss_count"])
        self.assertEqual(entry["repetition"], 0)  # SM-2 答错重置，顽固判定只看错次

    def test_telemetry_gamification_weekly_fields(self):
        """P0 批次：dashboard 含 AI 遥测/游戏化/周报；复习后 XP 生效；hint 调用记入遥测。"""
        pid = self._create_problem(title="P0遥测题", error_type="careless")
        # hint 无 AI 时走降级，仍记遥测（ok=0）
        self._request("POST", f"/api/problems/{pid}/hint", {"level": 2})
        _, _rv = self._request("GET", "/api/reviews"); reviews = _rv["items"]
        rid = next(r["id"] for r in reviews if r["problem_id"] == pid)
        self._request("POST", f"/api/reviews/{rid}/complete", {"rating": 4})
        status, d = self._request("GET", "/api/dashboard")
        self.assertEqual(status, 200)
        # 遥测：至少 1 次 hint 调用（无 AI 环境失败也计数）
        self.assertIn("telemetry", d)
        self.assertGreaterEqual(d["telemetry"]["calls"], 1)
        self.assertGreaterEqual(d["telemetry"]["failed"], 1)
        # 游戏化：rating=4 → 15 XP，连胜 ≥1，首战徽章解锁
        self.assertIn("gamification", d)
        self.assertGreaterEqual(d["gamification"]["total_xp"], 15)
        self.assertGreaterEqual(d["gamification"]["today_reviews"], 1)
        self.assertGreaterEqual(d["gamification"]["streak"], 1)
        first = next((b for b in d["gamification"]["badges"] if b["id"] == "first_blood"), None)
        self.assertIsNotNone(first)
        self.assertTrue(first["unlocked"])
        # 周报：本周有 1 题 1 复习
        self.assertIn("weekly", d)
        self.assertGreaterEqual(d["weekly"]["new_problems"], 1)
        self.assertGreaterEqual(d["weekly"]["week_reviews"], 1)

    def test_methods_roundtrip(self):
        """A8：methods 列创建时写入、详情返回、PUT 整体覆盖、非法输入忽略。"""
        pid = self._create_problem(title="A8多解法", content="求电路电流")
        status, _ = self._request("PUT", f"/api/problems/{pid}",
                                  {"methods": ["方法一：基尔霍夫方程", {"bad": "忽略"}]})
        self.assertEqual(status, 200)
        status, p = self._request("GET", f"/api/problems/{pid}")
        self.assertEqual(status, 200)
        self.assertIn("methods", p)
        self.assertEqual(p["methods"], ["方法一：基尔霍夫方程"])
        # 覆盖为空数组
        self._request("PUT", f"/api/problems/{pid}", {"methods": []})
        status, p = self._request("GET", f"/api/problems/{pid}")
        self.assertEqual(p["methods"], [])

    def test_analytics_forgetting_curve(self):
        """D4：完成 FSRS 复习后 analytics 含遗忘曲线（实测桶 + 预测曲线）。"""
        pid = self._create_problem(title="D4遗忘曲线", content="求电场强度")
        _, _rv = self._request("GET", "/api/reviews"); reviews = _rv["items"]
        rid = next(r["id"] for r in reviews if r["problem_id"] == pid)
        self._request("POST", f"/api/reviews/{rid}/complete", {"rating": 4})
        status, data = self._request("GET", "/api/analytics")
        self.assertEqual(status, 200)
        f = data["forgetting"]
        self.assertIn("buckets", f)
        self.assertIn("curve", f)
        self.assertGreaterEqual(sum(b["count"] for b in f["buckets"]), 1)
        self.assertGreater(f["avg_stability"], 0)
        self.assertGreater(len(f["curve"]), 2)
        self.assertLessEqual(f["curve"][0]["r"], 1.0)
        self.assertGreaterEqual(f["curve"][-1]["r"], 0.0)

    def test_hint_lang_en_fallback(self):
        """F2：hint 请求带 lang=en 时走英文降级提示。"""
        pid = self._create_problem(title="F2英文提示", content="求小球落地的速度")
        status, data = self._request("POST", f"/api/problems/{pid}/hint", {"level": 2, "lang": "en-US"})
        self.assertEqual(status, 200)
        self.assertEqual(data["source"], "fallback")
        self.assertTrue(data["content"])
        # 英文降级模板应包含英文引导而非中文
        self.assertNotIn("受力", data["content"])

    def test_locale_consistency(self):
        """F2：zh-CN/en-US 键集一致，且 /locale/*.json 可访问。"""
        base = Path(__file__).resolve().parent.parent / "static" / "locale"
        zh = json.load(open(base / "zh-CN.json", encoding="utf-8"))
        en = json.load(open(base / "en-US.json", encoding="utf-8"))
        self.assertEqual(set(zh.keys()), set(en.keys()))
        status, data = self._request("GET", "/locale/zh-CN.json")
        self.assertEqual(status, 200)
        self.assertIsInstance(data, dict)
        self.assertIn("nav.dashboard", data)
        status2, _ = self._request("GET", "/locale/en-US.json")
        self.assertEqual(status2, 200)

    def test_locale_keys_referenced_in_html_exist(self):
        """F2 守护：index/concept_map 中所有 data-i18n 引用的键必须存在于 locale 词条。"""
        import re
        base = Path(__file__).resolve().parent.parent / "static"
        zh = json.load(open(base / "locale" / "zh-CN.json", encoding="utf-8"))
        refs = set()
        for html_name in ("index.html", "concept_map.html"):
            html = open(base / html_name, encoding="utf-8").read()
            for attr in ("data-i18n", "data-i18n-ph", "data-i18n-aria"):
                refs.update(re.findall(attr + r'="([^"]+)"', html))
        missing = sorted(k for k in refs if k not in zh)
        self.assertEqual(missing, [])

    def test_html_visible_chinese_must_be_wired(self):
        """F2 反向守护：HTML 中所有可见中文文本必须带 data-i18n（防硬编码回退）。

        动态填充的占位元素（JS 首次加载即覆盖）与语言名选项除外。
        """
        import re
        from html.parser import HTMLParser
        base = Path(__file__).resolve().parent.parent / "static"

        # JS 首次加载即覆盖的占位元素（静态文本只是加载瞬间的占位）
        dynamic_ids = {
            "sprintCard", "todaySummaryText", "flashContent", "ocrProbe",
            "ollamaStatus", "fsrsStatus", "photoHint", "trendHint",
            "profileBox", "topicsList", "recentList", "courseStats",
            "recentActivity", "weeklyCard", "stubbornList", "errorDist",
            "errorTrend", "flashMeta", "flashAttempt", "flashFix",
            "methodsArea", "examList", "ragDocs", "ragResults", "ocrResult",
            "oralChat", "dueBadge", "batchBar",
        }

        class Scan(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack = []
                self.issues = []

            def handle_starttag(self, tag, attrs):
                self.stack.append({"tag": tag, "attrs": dict(attrs), "text": []})

            def handle_endtag(self, tag):
                if not self.stack:
                    return
                node = self.stack.pop()
                if node["tag"] != tag:
                    return
                text = "".join(node["text"]).strip()
                if not text or not re.search(r"[\u4e00-\u9fff]", text):
                    return
                attrs = node["attrs"]
                if any(k in attrs for k in ("data-i18n", "data-i18n-ph", "data-i18n-aria")):
                    return
                if node["tag"] in ("script", "style", "title"):
                    return
                if attrs.get("id") in dynamic_ids:
                    return
                if node["tag"] == "option" and attrs.get("value") == "zh-CN":
                    return  # 语言名自引用，不翻译
                self.issues.append((node["tag"], attrs.get("id", ""), text[:60]))

        for html_name in ("index.html", "concept_map.html"):
            scan = Scan()
            scan.feed(open(base / html_name, encoding="utf-8").read())
            self.assertEqual(scan.issues, [], f"{html_name} 存在未接线的中文文本: {scan.issues}")

    def test_write_idempotency(self):
        # 相同 X-Request-Id 重复提交应返回首次结果，不产生重复题目
        body = {"title": "幂等题", "content": "test"}
        rid = "idem-1"
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/problems", json.dumps(body).encode(),
                     {"Content-Type": "application/json", "X-Requested-With": "LearnOS", "X-Request-Id": rid})
        r1 = json.loads(conn.getresponse().read().decode())
        conn.request("POST", "/api/problems", json.dumps(body).encode(),
                     {"Content-Type": "application/json", "X-Requested-With": "LearnOS", "X-Request-Id": rid})
        r2 = json.loads(conn.getresponse().read().decode())
        conn.close()
        self.assertEqual(r1["id"], r2["id"])

    def test_keystore_unlock_clear_and_periodic_reports(self):
        """密钥保管（解锁/清除 keys.enc）+ 周期报告详情端点。"""
        # ── 密钥：写入 keys.enc → 重启（清会话）→ 解锁 → 清除 ──
        if not keystore.crypto_available():
            self.skipTest("cryptography 不可用")
        import ai
        orig_key_file = keystore.KEY_FILE
        keystore.KEY_FILE = Path(self._tmp.name) / "keys_test.enc"
        try:
            self.assertTrue(keystore.save_key("sk-unlock-test", "pw-ok"))
            ai.reset_session_key()  # 模拟重启：仅清内存，保留 keys.enc
            status, s = self._request("GET", "/api/settings")
            self.assertEqual(status, 200)
            self.assertTrue(s["key_file_locked"])
            # 错误口令 → 解锁失败
            status, r = self._request("POST", "/api/keystore/unlock", {"master_password": "wrong"})
            self.assertEqual(status, 200)
            self.assertFalse(r["ok"])
            # 正确口令 → 解锁成功，key_source=keyfile
            status, r = self._request("POST", "/api/keystore/unlock", {"master_password": "pw-ok"})
            self.assertEqual(status, 200)
            self.assertTrue(r["ok"])
            self.assertEqual(r["key_source"], "keyfile")
            self.assertFalse(r["key_file_locked"])
            # 清除 → keys.enc 删除
            status, r = self._request("POST", "/api/keystore/clear", {})
            self.assertEqual(status, 200)
            self.assertTrue(r["ok"])
            self.assertFalse(keystore.key_file_exists())
        finally:
            ai.reset_session_key()  # 仅清内存，避免误删真实 keys.enc
            keystore.KEY_FILE = orig_key_file
        # ── 周期报告：创建 1 题 1 复习后周报/月报应含数据 ──
        pid = self._create_problem(title="周期报告题", error_type="计算错误")
        _, _rv = self._request("GET", "/api/reviews"); reviews = _rv["items"]
        rid = next(r["id"] for r in reviews if r["problem_id"] == pid)
        self._request("POST", f"/api/reviews/{rid}/complete", {"rating": 4})
        status, w = self._request("GET", "/api/report/weekly")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(w["new_problems"], 1)
        self.assertGreaterEqual(w["week_reviews"], 1)
        self.assertTrue(w["tip_key"].startswith("report.tipWeek"))
        status, m = self._request("GET", "/api/report/monthly")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(m["month_revs"], 1)
        self.assertGreaterEqual(m["active_days"], 1)
        self.assertTrue(m["tip_key"].startswith("report.tipMonth"))
        self.assertIn("daily", m)
        # 30 天窗口外（40 天前）的复习不应计入近 30 天活跃天数/复习数
        from datetime import date, timedelta
        old = (date.today() - timedelta(days=40)).isoformat()
        with db.db() as conn:
            conn.execute(
                "INSERT INTO reviews(problem_id, due_date, interval_days, result, completed, created_at) "
                "VALUES (?, ?, 1, '4', 1, ?)", (pid, old, old))
        status, m2 = self._request("GET", "/api/report/monthly")
        self.assertEqual(status, 200)
        self.assertEqual(m2["active_days"], m["active_days"])
        self.assertEqual(m2["month_revs"], m["month_revs"])


if __name__ == "__main__":
    unittest.main()
