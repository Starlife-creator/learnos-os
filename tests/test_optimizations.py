"""针对 2026-08 优化的回归测试：concept_ids 格式 / 提示缓存开关 / CSP 头 / 脱敏扩展。"""
import json
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

_TEST_TMP_DIR = Path(__file__).resolve().parent / ".tmp"
_TEST_TMP_DIR.mkdir(exist_ok=True)


class TestOptimizations(unittest.TestCase):
    server: ThreadingHTTPServer
    thread: Thread

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="opt_", dir=_TEST_TMP_DIR)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "opt_test.db"
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
        csp = resp.getheader("Content-Security-Policy")
        conn.close()
        return resp.status, result, csp

    def _create_problem(self, extra=None):
        body = {"title": "测试题", "content": "1+1=?", "course": "测试", "topic": "t",
                "error_type": "概念不清", "subject": "physics"}
        body.update(extra or {})
        status, data, _ = self._request("POST", "/api/problems", body)
        self.assertIn(status, (200, 201), data)
        return data["id"]

    def test_csp_header(self):
        _, _, csp = self._request("GET", "/api/health")
        self.assertIsNotNone(csp)
        self.assertIn("default-src 'self'", csp)

    def test_concept_csv_format(self):
        pid = self._create_problem({"concept_ids": [3, 7]})
        with db.db() as conn:
            stored = conn.execute(
                "SELECT concept_ids FROM problems WHERE id = ?", (pid,)
            ).fetchone()[0]
        self.assertEqual(stored, ",3,7,")  # 规范格式，无双逗号

    def test_hint_cache_toggle(self):
        # 开启（默认）：AI 未配置 → 降级提示仍落库缓存
        pid = self._create_problem()
        status, data, _ = self._request("POST", f"/api/problems/{pid}/hint", {"level": 1})
        self.assertEqual(status, 200)
        self.assertEqual(data["source"], "fallback")
        self.assertTrue(data["cached"])
        with db.db() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM hints WHERE problem_id = ?", (pid,)
            ).fetchone()[0]
        self.assertEqual(n, 1)
        # 复读命中缓存
        _, data2, _ = self._request("POST", f"/api/problems/{pid}/hint", {"level": 1})
        self.assertEqual(data2["source"], "saved")
        self.assertTrue(data2["cached"])

        # 关闭后：不再落库
        self._request("PUT", "/api/settings", {"hint_cache_enabled": "0"})
        pid2 = self._create_problem()
        _, data3, _ = self._request("POST", f"/api/problems/{pid2}/hint", {"level": 1})
        self.assertFalse(data3["cached"])
        with db.db() as conn:
            n2 = conn.execute(
                "SELECT COUNT(*) FROM hints WHERE problem_id = ?", (pid2,)
            ).fetchone()[0]
        self.assertEqual(n2, 0)
        # 恢复默认，避免影响其他用例
        self._request("PUT", "/api/settings", {"hint_cache_enabled": "1"})

    def test_redactor_extended_patterns(self):
        from config import SecretRedactor
        redactor = SecretRedactor()

        class _Record:
            def __init__(self, msg):
                self.msg = msg
                self.args = ()

            def getMessage(self):
                return self.msg

        for raw in ("token=abcdef123456", "key='xyz987654321'",
                    "api_key=\"sk-test12345678\""):
            rec = _Record(raw)
            redactor.filter(rec)
            self.assertIn("***REDACTED***", rec.msg)
            self.assertNotIn("abcdef123456", rec.msg)
            self.assertNotIn("xyz987654321", rec.msg)


class TestSubjectAdmin(unittest.TestCase):
    """学科注册表：网页端增删 + 内置保护 + 有数据阻止删除。"""

    server: ThreadingHTTPServer
    thread: Thread

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="subj_", dir=_TEST_TMP_DIR)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "subj_test.db"
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

    def test_registry_seeded(self):
        _, data = self._request("GET", "/api/subjects")
        ids = {s["id"] for s in data["subjects"]}
        self.assertIn("physics", ids)
        self.assertIn("chemistry", ids)
        self.assertIn("math", ids)

    def test_add_and_delete_subject(self):
        status, data = self._request("POST", "/api/subjects",
                                     {"id": "zztemp_delme", "title": "临时测试学科"})
        self.assertEqual(status, 201)
        _, listing = self._request("GET", "/api/subjects")
        entry = next(s for s in listing["subjects"] if s["id"] == "zztemp_delme")
        self.assertEqual(entry["title"], "临时测试学科")
        self.assertFalse(entry["builtin"])
        # 重复添加 → 409
        status, _, = self._request("POST", "/api/subjects", {"id": "zztemp_delme"})
        self.assertEqual(status, 409)
        # 非法 id → 400
        status, _ = self._request("POST", "/api/subjects", {"id": "1bad id"})
        self.assertEqual(status, 400)
        # 无数据自建学科可删除
        status, data = self._request("DELETE", "/api/subjects/zztemp_delme")
        self.assertEqual(status, 200)
        _, listing = self._request("GET", "/api/subjects")
        self.assertNotIn("zztemp_delme", {s["id"] for s in listing["subjects"]})

    def test_builtin_and_data_protection(self):
        status, _ = self._request("DELETE", "/api/subjects/physics")
        self.assertEqual(status, 400)  # 内置不可删
        self._request("POST", "/api/subjects", {"id": "history"})
        conn_body = {"title": "h", "content": "c", "course": "x", "topic": "t",
                     "error_type": "概念不清", "subject": "history"}
        self._request("POST", "/api/problems", conn_body)
        status, data = self._request("DELETE", "/api/subjects/history")
        self.assertEqual(status, 409)  # 有数据阻止
        self.assertIn("problems", data["error"])


if __name__ == "__main__":
    unittest.main()
