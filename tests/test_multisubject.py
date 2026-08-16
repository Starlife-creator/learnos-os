"""多学科隔离测试：三学科种子加载、数据隔离、跨学科同名概念、自建学科、HTTP 端点。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from http.client import HTTPConnection
from threading import Thread
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
import graph
import bank
from handler import Handler

_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)


class _Server(ThreadingHTTPServer):
    daemon_threads = True


class TestMultiSubject(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="multi_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "multi_test.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()
        cls._server = _Server(("127.0.0.1", 0), Handler)
        cls._port = cls._server.server_address[1]
        cls._thread = Thread(target=cls._server.serve_forever, daemon=True)
        cls._thread.start()

    @classmethod
    def tearDownClass(cls):
        cls._server.shutdown()
        cls._server.server_close()
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db
        cls._tmp.cleanup()

    def _request(self, method, path, body=None):
        conn = HTTPConnection("127.0.0.1", self._port, timeout=10)
        headers = {"X-Requested-With": "LearnOS"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return resp.status, {}

    def _create_problem(self, subject, title):
        return self._request("POST", f"/api/problems?subject={subject}", {
            "title": title, "content": "题目内容", "course": "C", "topic": "T",
            "error_type": "calculation",
        })

    def test_builtin_seeds_loaded(self):
        for sid, expect_min in (("physics", 300), ("chemistry", 70), ("math", 60)):
            status, data = self._request("GET", f"/api/graph/concepts?subject={sid}")
            self.assertEqual(status, 200)
            self.assertGreater(len(data["nodes"]), expect_min, f"{sid} 节点不足")
            self.assertEqual(data["subject"], sid)

    def test_problems_isolated_by_subject(self):
        status, created = self._create_problem("chemistry", "化学题-A")
        self.assertEqual(status, 201)
        status, chem = self._request("GET", "/api/problems?subject=chemistry")
        self.assertGreaterEqual(chem["total"], 1)
        status, phys = self._request("GET", "/api/problems?subject=physics")
        for item in phys.get("items", []):
            self.assertNotEqual(item["title"], "化学题-A")

    def test_dashboard_isolated(self):
        self._create_problem("math", "数学题-B")
        status, m = self._request("GET", "/api/dashboard?subject=math")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(m["stats"]["total"], 1)
        status, p = self._request("GET", "/api/dashboard?subject=physics")
        self.assertEqual(status, 200)
        self.assertEqual(p["stats"]["total"], 0)

    def test_custom_subject_accepted(self):
        status, data = self._request("GET", "/api/subjects")
        self.assertEqual(status, 200)
        ids = [s["id"] for s in data["subjects"]]
        for bid in ("physics", "chemistry", "math"):
            self.assertIn(bid, ids)
        # 注册表驱动：先注册自建学科，再访问（graph 无种子则节点为空，不应 500）
        status, _ = self._request("POST", "/api/subjects", {"id": "mycustom"})
        self.assertEqual(status, 201)
        status, g = self._request("GET", "/api/graph/concepts?subject=mycustom")
        self.assertEqual(status, 200)
        self.assertEqual(g["subject"], "mycustom")
        # 未注册的合法 id 回退默认学科
        status, g = self._request("GET", "/api/graph/concepts?subject=unregistered")
        self.assertEqual(status, 200)
        self.assertEqual(g["subject"], "physics")

    def test_invalid_subject_falls_back(self):
        status, g = self._request("GET", "/api/graph/concepts?subject=bad%20id%21")
        self.assertEqual(status, 200)
        self.assertEqual(g["subject"], "physics")

    def test_same_name_different_subjects(self):
        status, _ = self._request("POST", "/api/graph/concepts?subject=chemistry", {
            "name": "同名概念-测试", "parent_id": 0,
        })
        self.assertEqual(status, 200)
        status, _ = self._request("POST", "/api/graph/concepts?subject=physics", {
            "name": "同名概念-测试", "parent_id": 0,
        })
        self.assertEqual(status, 200)

    def test_settings_default_subject(self):
        status, r = self._request("PUT", "/api/settings", {"default_subject": "chemistry"})
        self.assertEqual(status, 200)
        status, s = self._request("GET", "/api/settings")
        self.assertEqual(s.get("default_subject"), "chemistry")

    def test_bank_isolated(self):
        from config import BUNDLE_ROOT
        status, q = self._request("GET", "/api/bank?subject=math")
        self.assertEqual(status, 200)
        self.assertIn("stats", q)
        self.assertIn("items", q)
        # bank 缓存按学科隔离：math 载入不影响 chemistry 的空缓存
        status, c = self._request("GET", "/api/bank?subject=chemistry")
        self.assertEqual(status, 200)


class TestGraphMultiSubjectCore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="graphmulti_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "graphmulti.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db
        cls._tmp.cleanup()

    def test_subject_seed_paths(self):
        self.assertEqual(graph.subject_seed_path("physics").name, "seed_concepts.json")
        self.assertEqual(graph.subject_seed_path("chemistry").name, "seed_concepts_chemistry.json")
        self.assertEqual(graph.subject_seed_path("mycustom").name, "seed_concepts_mycustom.json")

    def test_update_progress_subject_arg(self):
        # 无崩溃即通过（三学科都能跑）
        graph.update_progress("physics")
        graph.update_progress("chemistry")
        graph.update_progress("math")

    def test_add_concept_subject(self):
        cid = graph.add_concept("新增概念", 0, subject="chemistry")
        self.assertIsNotNone(cid)
        self.assertIsNone(graph.add_concept("新增概念", 0, subject="chemistry"))
        # 同名不同学科允许
        cid2 = graph.add_concept("新增概念", 0, subject="math")
        self.assertIsNotNone(cid2)


if __name__ == "__main__":
    unittest.main()