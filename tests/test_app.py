"""端到端集成测试：完整学习循环。"""
import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from http.server import ThreadingHTTPServer

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
from handler import Handler


class PhysicsStudyOSTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls.temp_dir.name) / "test.db"
        db.init_db()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        config.DB_PATH = cls._orig_db
        cls.temp_dir.cleanup()

    def request(self, path, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def text_request(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as response:
            return response.status, response.read().decode("utf-8")

    def test_01_health_and_static(self):
        status, data = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

        page_status, page = self.text_request("/")
        self.assertEqual(page_status, 200)
        self.assertIn("物理学习", page)

    def test_02_problem_learning_loop(self):
        # 创建题目
        status, created = self.request("/api/problems", "POST", {
            "title": "测试单摆题",
            "course": "经典力学",
            "topic": "简谐振动",
            "content": "求小角度单摆周期。",
            "my_attempt": "从切向受力开始。",
            "error_type": "公式适用条件",
            "mastery": 2,
        })
        self.assertEqual(status, 201)
        problem_id = created["id"]

        # 获取题目详情
        _, problem = self.request(f"/api/problems/{problem_id}")
        self.assertEqual(problem["topic"], "简谐振动")

        # 获取一级提示（无 AI 配置，应为 fallback）
        _, hint = self.request(f"/api/problems/{problem_id}/hint", "POST", {"level": 1})
        self.assertEqual(hint["source"], "fallback")
        self.assertIn("量纲", hint["content"])

        # 获取今日复习列表
        _, reviews = self.request("/api/reviews")
        review = next(item for item in reviews if item["problem_id"] == problem_id)

        # 完成复习（rating=3，SM-2 首次记住 interval=1）
        _, result = self.request(f"/api/reviews/{review['id']}/complete", "POST", {"rating": 3})
        self.assertGreaterEqual(result["interval_days"], 1)

        # 再次完成复习（rating=4，第二次记住 interval=3）
        _, reviews2 = self.request("/api/reviews")
        review2 = next(item for item in reviews2 if item["problem_id"] == problem_id)
        _, result2 = self.request(f"/api/reviews/{review2['id']}/complete", "POST", {"rating": 4})
        self.assertGreaterEqual(result2["interval_days"], 3)

    def test_03_settings_secret_is_masked(self):
        self.request("/api/settings", "PUT", {
            "api_base": "https://example.invalid/v1",
            "api_key": "secret-test-key",
            "model": "test-model",
            "temperature": "0.2",
        })
        _, settings = self.request("/api/settings")
        self.assertEqual(settings["api_key"], "••••••••")
        self.assertTrue(settings["has_api_key"])

    def test_04_delete_cascades(self):
        status, created = self.request("/api/problems", "POST", {
            "title": "待删除",
            "content": "test",
        })
        pid = created["id"]
        status, _ = self.request(f"/api/problems/{pid}", "DELETE")
        self.assertEqual(status, 200)
        import urllib.error
        try:
            self.request(f"/api/problems/{pid}")
            self.fail("Should have raised HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_05_dashboard(self):
        status, data = self.request("/api/dashboard")
        self.assertEqual(status, 200)
        self.assertIn("stats", data)
        self.assertIn("due", data)
        self.assertIn("topics", data)
        self.assertIn("recent", data)


if __name__ == "__main__":
    unittest.main()
