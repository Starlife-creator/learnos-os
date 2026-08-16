"""A2 概念知识图谱测试：seed 加载、掌握度传播（3 类用例）、先修告警、端点。"""
import json
import tempfile
import unittest
from pathlib import Path
from http.client import HTTPConnection
from threading import Thread
from http.server import ThreadingHTTPServer

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
import graph
from handler import Handler

_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)


class TestGraphCore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="graph_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "graph_test.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db
        cls._tmp.cleanup()

    def _make_problem(self, title, topic, content, mastery):
        with db.DB_LOCK, db.db() as conn:
            cur = conn.execute(
                "INSERT INTO problems(title, topic, content, mastery, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (title, topic, content, mastery, db.now(), db.now()),
            )
            return int(cur.lastrowid)

    def test_seed_loaded(self):
        data = graph.load_graph()
        self.assertGreater(len(data["nodes"]), 300)
        self.assertGreater(len(data["links"]), 100)
        levels = {n["level"] for n in data["nodes"]}
        self.assertEqual(levels, {0, 1, 2})
        # 对比关系存在（验收用例 3）
        contrast = [l for l in data["links"] if l["relation"] == "contrast"]
        self.assertGreater(len(contrast), 0)
        # 幂等：重复调用不重复加载
        graph.ensure_seed()
        data2 = graph.load_graph()
        self.assertEqual(len(data2["nodes"]), len(data["nodes"]))

    def test_mastery_no_prereq(self):
        """验收用例 1：无先修概念 → 掌握度 = 自身聚合。"""
        pid = self._make_problem("参考系题", "参考系与坐标系", "关于参考系的题目", 4)
        cid = graph.bind_problem(pid)[0]
        graph.update_progress()
        prog = db.row("SELECT mastery, reviews FROM concept_progress WHERE concept_id = ?", (cid,))
        self.assertEqual(prog["reviews"], 1)
        self.assertAlmostEqual(prog["mastery"], 0.8, places=2)

    def test_mastery_prereq_weak(self):
        """验收用例 2：先修不熟 → 子概念掌握度打折。"""
        # 找到「简谐运动」及其先修「牛顿第二定律」
        shm = db.row("SELECT id FROM concepts WHERE name = '简谐运动'")
        newt = db.row("SELECT id FROM concepts WHERE name = '牛顿第二定律'")
        self.assertIsNotNone(shm)
        self.assertIsNotNone(newt)
        # 先修题掌握度 1/5（0.2 < 0.6），子概念题掌握度 5/5（自身 1.0）
        self._make_problem("牛顿二题", "牛顿第二定律", "牛顿第二定律的题目", 1)
        self._make_problem("简谐题", "简谐运动", "简谐运动的题目", 5)
        graph.bind_problem(db.row("SELECT id FROM problems WHERE title = '牛顿二题'")["id"])
        graph.bind_problem(db.row("SELECT id FROM problems WHERE title = '简谐题'")["id"])
        graph.update_progress()
        prog = db.row("SELECT mastery FROM concept_progress WHERE concept_id = ?", (shm["id"],))
        # 先修门传播后：0 < 打折值 < 自身 1.0（且明显低于 0.8）
        self.assertGreater(prog["mastery"], 0.0)
        self.assertLess(prog["mastery"], 0.8)
        # 无绑定概念不受影响：牛顿第二定律自身掌握度 = 0.2（0.2 < 0.6）
        newt_prog = db.row("SELECT mastery FROM concept_progress WHERE concept_id = ?", (newt["id"],))
        self.assertLess(newt_prog["mastery"], 0.6)

    def test_prereq_warnings_endpoint_data(self):
        """先修告警：绑定「简谐运动」的题应告警「牛顿第二定律 掌握度低」。"""
        pid = self._make_problem("简谐题2", "简谐运动", "简谐运动的题目", 5)
        graph.bind_problem(pid)
        warnings = graph.prereq_warnings(pid)
        names = [w["name"] for w in warnings]
        self.assertIn("牛顿第二定律", names)
        self.assertLess(warnings[0]["mastery"], 60)

    def test_user_add_delete_concept(self):
        cid = graph.add_concept("测试自定义概念", 0)
        self.assertIsNotNone(cid)
        node = db.row("SELECT * FROM concepts WHERE id = ?", (cid,))
        self.assertEqual(node["user_edited"], 1)
        self.assertTrue(graph.delete_concept(cid))
        self.assertFalse(graph.delete_concept(cid))

    def test_prereq_chain_and_problems(self):
        chain = graph.prereq_chain(db.row("SELECT id FROM concepts WHERE name = '简谐运动'")["id"])
        self.assertIn(db.row("SELECT id FROM concepts WHERE name = '牛顿第二定律'")["id"], chain)
        items = graph.problems_for_concepts(chain)
        titles = {i["title"] for i in items}
        self.assertIn("简谐题", titles)
        self.assertIn("牛顿二题", titles)


class TestGraphEndpoints(unittest.TestCase):
    server: ThreadingHTTPServer

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="graph_http_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "graph_http.db"
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

    def _req(self, method, path, body=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = json.dumps(body) if body else None
        conn.request(method, path, data, {"Content-Type": "application/json", "X-Requested-With": "LearnOS"})
        resp = conn.getresponse()
        result = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, result

    def test_graph_endpoints(self):
        status, data = self._req("GET", "/api/graph/concepts")
        self.assertEqual(status, 200)
        self.assertGreater(len(data["nodes"]), 300)
        node = next(n for n in data["nodes"] if n["name"] == "简谐运动")
        self.assertIn("level", node)
        self.assertIn("mastery_est", node)
        self.assertIn("looms_in", node)
        # 新增概念
        status, created = self._req("POST", "/api/graph/concepts", {"name": "端点测试概念"})
        self.assertEqual(status, 200)
        cid = created["id"]
        # 先修模式过滤
        status, data = self._req("GET", f"/api/graph/problems?concept={node['id']}")
        self.assertEqual(status, 200)
        self.assertIsInstance(data["items"], list)
        # 删除
        status, data = self._req("DELETE", f"/api/graph/concepts/{cid}")
        self.assertEqual(status, 200)

    def test_problem_auto_bind_and_detail(self):
        status, created = self._req("POST", "/api/problems", {
            "title": "单摆周期计算", "topic": "单摆", "content": "求单摆周期",
        })
        pid = created["id"]
        status, detail = self._req("GET", f"/api/problems/{pid}")
        self.assertEqual(status, 200)
        self.assertIsInstance(detail["concept_ids"], list)
        self.assertGreaterEqual(len(detail["concept_ids"]), 1)
        names = [n["name"] for n in self._req("GET", "/api/graph/concepts")[1]["nodes"]]
        bound_names = [names[cid - 1] if 0 < cid <= len(names) else "" for cid in detail["concept_ids"]]
        self.assertIn("单摆", bound_names)
        self.assertIsInstance(detail["prereq_warnings"], list)

    def test_list_prereq_filter(self):
        status, created = self._req("POST", "/api/problems", {
            "title": "先修过滤题", "topic": "简谐运动", "content": "简谐运动相关",
        })
        pid = created["id"]
        detail = self._req("GET", f"/api/problems/{pid}")[1]
        cid = detail["concept_ids"][0]
        status, data = self._req("GET", f"/api/problems?prereq={cid}")
        self.assertEqual(status, 200)
        titles = [i["title"] for i in data["items"]]
        self.assertIn("先修过滤题", titles)


if __name__ == "__main__":
    unittest.main()
