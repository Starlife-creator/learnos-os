"""测试 HTTP 端点的基本 CRUD 路径。"""
import json
import os
import sys
import tempfile
import unittest
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
from handler import Handler


class TestEndpoints(unittest.TestCase):
    server: ThreadingHTTPServer
    thread: Thread

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp()
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmpdir) / "test_api.db"
        config.PORT = 18765
        db.init_db()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 18765), Handler)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        config.DB_PATH = cls._orig_db

    def _request(self, method, path, body=None):
        conn = HTTPConnection("127.0.0.1", 18765, timeout=5)
        data = json.dumps(body) if body else None
        conn.request(method, path, data, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        result = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, result

    def test_health(self):
        status, data = self._request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_dashboard_empty(self):
        status, data = self._request("GET", "/api/dashboard")
        self.assertEqual(status, 200)
        self.assertIn("stats", data)
        self.assertIn("due", data)

    def test_create_problem(self):
        status, data = self._request("POST", "/api/problems", {
            "title": "端到端测试题",
            "course": "力学",
            "topic": "能量守恒",
            "content": "求小球滑下斜面后的速度",
        })
        self.assertEqual(status, 201)
        self.assertIn("id", data)
        return data["id"]

    def test_create_validation(self):
        status, data = self._request("POST", "/api/problems", {"title": "", "content": ""})
        self.assertEqual(status, 400)

    def test_get_problem(self):
        pid = self.test_create_problem()
        status, data = self._request("GET", f"/api/problems/{pid}")
        self.assertEqual(status, 200)
        self.assertEqual(data["title"], "端到端测试题")
        self.assertIn("hints", data)

    def test_get_problem_not_found(self):
        status, data = self._request("GET", "/api/problems/99999")
        self.assertEqual(status, 404)

    def test_update_problem(self):
        pid = self.test_create_problem()
        status, data = self._request("PUT", f"/api/problems/{pid}", {"mastery": 4})
        self.assertEqual(status, 200)
        status, data = self._request("GET", f"/api/problems/{pid}")
        self.assertEqual(data["mastery"], 4)

    def test_delete_problem(self):
        pid = self.test_create_problem()
        status, data = self._request("DELETE", f"/api/problems/{pid}")
        self.assertEqual(status, 200)
        status, data = self._request("GET", f"/api/problems/{pid}")
        self.assertEqual(status, 404)

    def test_reviews_empty(self):
        status, data = self._request("GET", "/api/reviews")
        self.assertEqual(status, 200)
        self.assertIsInstance(data, list)

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
        conn = HTTPConnection("127.0.0.1", 18765, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertIn("物理学习", body)


if __name__ == "__main__":
    unittest.main()
