"""A2 概念知识图谱测试：seed 加载、掌握度传播（3 类用例）、先修告警、端点。"""
import json
import tempfile
import unittest
from datetime import date
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
        graph.invalidate_progress_cache()  # 新会话：清掉跨测试残留的掌握度 TTL 缓存

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

    def test_subject_id_normalization(self):
        """守护：学科 id 大小写归一，杜绝 Title-case 双副本空壳科（2026-08-23 踩坑）。

        任意大小写变体查询/加载都命中同一小写学科，且不产生独立大写壳。
        """
        # 临时库由 init_db→register_builtin_subjects 加载 data/seed_concepts_* 全部小写学科
        self.assertTrue(db.subject_exists("biology"), "临时库应已注册 biology 种子学科")
        # 大小写变体查同一科
        self.assertTrue(db.subject_exists("Biology"))
        self.assertTrue(db.subject_exists("BIOLOGY"))
        # 加载大小写变体不报错且返回同一学科节点集（不产生新壳）
        g_lower = graph.load_graph("biology")
        g_upper = graph.load_graph("BIOLOGY")
        self.assertGreater(len(g_lower["nodes"]), 0)
        self.assertEqual(len(g_upper["nodes"]), len(g_lower["nodes"]))
        # subjects 表里不存在独立的大写壳条目
        ids = {s["id"] for s in db.list_subjects()}
        self.assertNotIn("Biology", ids)
        self.assertIn("biology", ids)

    def test_mastery_no_prereq(self):
        """验收用例 1：无先修概念 → 掌握度 = 自身聚合（3 份证据越过 M1 封顶线，验证纯聚合）。"""
        for i in range(3):
            pid = self._make_problem(f"参考系题{i}", "参考系与坐标系", "关于参考系的题目", 4)
            cid = graph.bind_problem(pid)[0]
        graph.update_progress()
        prog = db.row("SELECT mastery, reviews FROM concept_progress WHERE concept_id = ?", (cid,))
        self.assertEqual(prog["reviews"], 3)
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

    def _leaf_parent(self) -> int:
        """取一个有子概念的单元 id 作新概念的父（掌握度只算 parent_id<>0 的概念）。"""
        graph.ensure_seed()  # 本类首个执行的用例可能先于任何 load_graph 调用
        return db.row("SELECT parent_id AS p FROM concepts WHERE parent_id <> 0 LIMIT 1")["p"]

    def test_delete_concept_trash_restore(self):
        """D3：概念删除入回收站，恢复时概念 + 关系边 + 掌握度行原样回来。"""
        import trash
        parent = self._leaf_parent()
        a = graph.add_concept("回收站概念A", parent)
        b = graph.add_concept("回收站概念B", parent)
        ok, err = graph.link_concepts(a, b, "prerequisite", reason="测试回收站级联")
        self.assertTrue(ok, err)
        pid = self._make_problem("回收站题", "回收站概念A", "内容", 4)
        self.assertTrue(graph.bind_concept(pid, a))
        self.assertTrue(graph.delete_concept(a))
        self.assertIsNone(db.row("SELECT * FROM concepts WHERE id = ?", (a,)))
        self.assertEqual(db.row(
            "SELECT COUNT(*) AS c FROM concept_links WHERE concept_a = ? OR concept_b = ?",
            (a, a))["c"], 0, "删除应级联清掉关系边")
        self.assertEqual(db.row(
            "SELECT COUNT(*) AS c FROM concept_progress WHERE concept_id = ?", (a,))["c"], 0)
        # 恢复：三表原样回来（含先修边）
        t = trash.restore("concept", a)
        self.assertIsNotNone(t)
        node = db.row("SELECT * FROM concepts WHERE id = ?", (a,))
        self.assertEqual(node["name"], "回收站概念A")
        self.assertEqual(db.row(
            "SELECT COUNT(*) AS c FROM concept_links WHERE concept_a = ? OR concept_b = ?",
            (a, a))["c"], 1, "恢复应带回关系边")
        self.assertEqual(db.row(
            "SELECT COUNT(*) AS c FROM concept_progress WHERE concept_id = ?", (a,))["c"], 1)
        # 无快照可恢复时显式失败
        self.assertFalse(graph.delete_concept(999999))
        self.assertIsNone(trash.restore("concept", 999999))

    def test_mastery_events_audit_trail(self):
        """D4 事件溯源：掌握度变化记录何时/入口/证据/前后值/revision；无变化不记。

        证据逐次累加会穿过 M1 封顶档：bind#1 0→0.5（1 次证据封顶）、
        bind#2 0.5→0.6（2 次证据 0.6<0.8 不封顶）、bind#3 无变化不记。"""
        parent = self._leaf_parent()
        cid = graph.add_concept("事件溯源概念", parent)
        pids = [self._make_problem(f"事件题{i}", "事件溯源概念", "内容", 3) for i in range(3)]
        for pid in pids:
            self.assertTrue(graph.bind_concept(pid, cid))
        ev = db.rows("SELECT * FROM mastery_events WHERE concept_id = ? ORDER BY id", (cid,))
        self.assertEqual(len(ev), 2, "1→2 证据穿过封顶档应各记一条，第 3 次绑定无变化不记")
        self.assertEqual(ev[0]["entry_point"], "bind")
        self.assertIn(f"绑定概念#{cid}", ev[0]["evidence"])
        self.assertEqual(ev[0]["prev_mastery"], 0.0)
        self.assertAlmostEqual(ev[0]["cur_mastery"], 0.5, places=2)  # 1 次证据封顶 0.5
        self.assertEqual(ev[1]["entry_point"], "bind")
        self.assertAlmostEqual(ev[1]["prev_mastery"], 0.5, places=2)
        self.assertAlmostEqual(ev[1]["cur_mastery"], 0.6, places=2)  # 2 次证据不封顶
        # 无变化的重算不产生新事件（守恒：重算收敛器不刷日志）
        graph.update_progress(entry_point="review", evidence="无变化重算")
        self.assertEqual(len(db.rows(
            "SELECT id FROM mastery_events WHERE concept_id = ?", (cid,))), 2)
        # 掌握度变化 → revision 递增，prev 接力上次 cur（3 份证据越过封顶线，0.6→1.0 可见）
        with db.DB_LOCK, db.db() as conn:
            conn.executemany(
                "UPDATE problems SET mastery = 5 WHERE id = ?",
                [(pid,) for pid in pids])
        graph.update_progress(entry_point="review", evidence=f"题目#{pids[0]} 评分4")
        ev2 = db.rows("SELECT * FROM mastery_events WHERE concept_id = ? ORDER BY id", (cid,))
        self.assertEqual(len(ev2), 3)
        self.assertEqual(ev2[2]["entry_point"], "review")
        self.assertIn("评分4", ev2[2]["evidence"])
        self.assertEqual(ev2[2]["revision"], 3, "同概念第三次变化 revision 应递增")
        self.assertAlmostEqual(ev2[2]["prev_mastery"], 0.6, places=2)
        self.assertAlmostEqual(ev2[2]["cur_mastery"], 1.0, places=2)

    def test_mastery_evidence_caps_m1(self):
        """M1 置信度封顶：1 次证据 ≤0.5、2 次 ≤0.8、≥3 次不封顶（防单次蒙对即掌握）。"""
        parent = self._leaf_parent()
        cid = graph.add_concept("封顶概念", parent)
        expect = [(1, 0.5), (2, 0.8), (3, 1.0)]
        for i, (_n, capped) in enumerate(expect, 1):
            pid = self._make_problem(f"封顶题{i}", "封顶概念", "内容", 5)
            self.assertTrue(graph.bind_concept(pid, cid))
            prog = db.row("SELECT mastery FROM concept_progress WHERE concept_id = ?", (cid,))
            self.assertAlmostEqual(prog["mastery"], capped, places=2,
                                   msg=f"{i} 次证据掌握度应封顶 {capped}")
        # 阈值可经 settings 调回：mastery_evidence_caps="0.9,0.95" → 新概念 1 次证据 0.9
        #（测毕删除该设置，避免泄漏到同类其他用例的默认封顶假设）
        with db.DB_LOCK, db.db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES ('mastery_evidence_caps', '0.9,0.95')")
        try:
            cid2 = graph.add_concept("封顶概念2", parent)
            pid = self._make_problem("封顶题X", "封顶概念2", "内容", 5)
            self.assertTrue(graph.bind_concept(pid, cid2))
            prog = db.row("SELECT mastery FROM concept_progress WHERE concept_id = ?", (cid2,))
            self.assertAlmostEqual(prog["mastery"], 0.9, places=2, msg="自定义封顶阈值应生效")
        finally:
            with db.DB_LOCK, db.db() as conn:
                conn.execute("DELETE FROM settings WHERE key = 'mastery_evidence_caps'")

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

    def test_next_step_endpoint(self):
        """U1：/api/learn/next-step 只读返回 next+queue，结构契约稳定。"""
        status, ns = self._req("GET", "/api/learn/next-step")
        self.assertEqual(status, 200)
        self.assertIn("next", ns)
        self.assertIn("queue", ns)
        if ns["queue"]:
            self.assertEqual(ns["next"], ns["queue"][0], "next 必须是队列首项")
            for q in ns["queue"]:
                self.assertIn(q["action"], ("review", "cards", "oral", "learn"))
                self.assertTrue(q["label_key"])
        else:
            self.assertEqual(ns["next"]["action"], "done")


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


class TestNextStep(unittest.TestCase):
    """U1 统一 next_step：状态驱动优先级 + 完成后前进（非游标）。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="next_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "next_test.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()
        graph.invalidate_progress_cache()

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db
        cls._tmp.cleanup()

    def _problem(self, topic: str, mastery: float, error_type: str = "") -> int:
        with db.DB_LOCK, db.db() as conn:
            cur = conn.execute(
                "INSERT INTO problems(title, topic, content, mastery, error_type, subject, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'physics', ?, ?)",
                ("U1测试题", topic, "内容内容内容内容", mastery, error_type, db.now(), db.now()))
            return int(cur.lastrowid)

    def _due_review(self, pid: int) -> None:
        with db.DB_LOCK, db.db() as conn:
            conn.execute(
                "INSERT INTO reviews(problem_id, due_date, interval_days, created_at) "
                "VALUES (?, ?, 1, ?)",
                (pid, date.today().isoformat(), db.now()))

    def _due_card(self) -> int:
        with db.DB_LOCK, db.db() as conn:
            cid = conn.execute(
                "SELECT id FROM concepts WHERE subject = 'physics' AND parent_id <> 0 LIMIT 1"
            ).fetchone()["id"]
            cur = conn.execute(
                "INSERT INTO cards(subject, concept_id, kind, cue, answer, status, due_date, created_at) "
                "VALUES ('physics', ?, 'qa', ?, 'U1答案', 'active', ?, ?)",
                (cid, f"U1线索{db.now()}", date.today().isoformat(), db.now()))
            return int(cur.lastrowid)

    def test_priority_order_and_advance(self):
        """到期错题 → 到期闪卡优先；完成到期错题后 next 前进到闪卡。"""
        ns = graph.next_step("physics")
        self.assertNotIn("review", [q["action"] for q in ns["queue"]])
        self.assertNotIn("cards", [q["action"] for q in ns["queue"]])
        pid = self._problem(topic="U1力学", mastery=3)
        self._due_review(pid)
        card_id = self._due_card()
        ns = graph.next_step("physics")
        self.assertEqual(ns["next"]["action"], "review", "到期错题最优先")
        self.assertEqual(ns["next"]["n"], 1)
        self.assertEqual(ns["next"]["label_key"], "queue.reviewDue")
        self.assertEqual([q["action"] for q in ns["queue"][:2]], ["review", "cards"],
                         "到期闪卡次之")
        # 完成到期错题复习（状态变化）→ next 前进，无需任何游标
        with db.DB_LOCK, db.db() as conn:
            conn.execute("UPDATE reviews SET completed = 1, result = '3' WHERE problem_id = ?", (pid,))
        ns = graph.next_step("physics")
        self.assertEqual(ns["next"]["action"], "cards", "完成后 next 自然前进")
        # 闪卡下线 → cards 步从队列消失
        with db.DB_LOCK, db.db() as conn:
            conn.execute("UPDATE cards SET status = 'suspended' WHERE id = ?", (card_id,))
        ns = graph.next_step("physics")
        self.assertNotIn("cards", [q["action"] for q in ns["queue"]])

    def test_oral_weak_topic_weighted(self):
        """M2：薄弱主题口试进队列，错因权重高者优先（空白 3 > 粗心 0）。"""
        self._problem(topic="U1甲", mastery=1, error_type="blank_in_facts")
        self._problem(topic="U1乙", mastery=2, error_type="careless")
        ns = graph.next_step("physics")
        oral = [q for q in ns["queue"] if q["action"] == "oral"]
        self.assertEqual(len(oral), 1)
        self.assertEqual(oral[0]["topic"], "U1甲")
        self.assertEqual(oral[0]["label_key"], "queue.oralWeak")
        # weak_oral_topic 抽取后与队列口径一致
        w = graph.weak_oral_topic("physics")
        self.assertEqual(w["topic"], "U1甲")

    def test_done_when_no_seed_subject(self):
        """无种子学科（无概念/无题/无卡）→ 队列空，next=done。"""
        ns = graph.next_step("u1noseed")
        self.assertEqual(ns["queue"], [])
        self.assertEqual(ns["next"]["action"], "done")
        self.assertEqual(ns["next"]["label_key"], "queue.allDone")


if __name__ == "__main__":
    unittest.main()
