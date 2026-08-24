"""v25 概念闪卡测试：卡片 CRUD、草稿生成（离线）、FSRS 调度、级联删除。"""
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
import cards
import graph

_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)


class TestCards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="cards_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "cards_test.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()
        graph.load_graph("math")  # 触发种子加载，提供概念
        nodes = [n for n in graph.load_graph("math")["nodes"] if n["level"] >= 1]
        cls.concept_id = nodes[0]["id"]
        cls.concept_name = nodes[0]["name"]

    @classmethod
    def tearDownClass(cls):
        db.close_all_connections()  # 释放临时库句柄，避免 Windows 文件锁阻碍清理
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db
        cls._tmp.cleanup()

    def _create(self, **kw):
        return cards.create_card(
            None, "math", kw.get("concept_id", self.concept_id),
            kw.get("cue", "什么是X？"), kw.get("answer", "X的定义。"),
            kw.get("kind", "qa"), kw.get("source", "manual"), kw.get("status", "active"))

    def test_create_and_list(self):
        cid = self._create(cue="唯一 cue A", answer="答案A")
        items = cards.list_cards("math")
        hit = [it for it in items if it["id"] == cid][0]
        self.assertEqual(hit["cue"], "唯一 cue A")
        self.assertEqual(hit["concept_name"], self.concept_name)
        self.assertEqual(hit["status"], "active")

    def test_create_requires_cue(self):
        with self.assertRaises(ValueError):
            cards.create_card(None, "math", self.concept_id, "", "答案", "qa", "manual", "active")

    def test_stats_counts(self):
        before = cards.stats("math")["total"]
        self._create(cue=f"统计卡{before}")
        after = cards.stats("math")
        self.assertEqual(after["total"], before + 1)
        self.assertGreaterEqual(after["active"], 1)

    def test_offline_drafts_valid_concept(self):
        drafts = cards.generate_drafts("math", self.concept_id, use_ai=False)
        self.assertTrue(drafts)
        kinds = {d["kind"] for d in drafts}
        self.assertTrue({"qa", "cloze"}.issubset(kinds))
        for d in drafts:
            self.assertTrue(d["cue"] and d["answer"])

    def test_generate_drafts_invalid_concept(self):
        with self.assertRaises(ValueError):
            cards.generate_drafts("math", 999999, use_ai=False)

    def test_review_advances_due(self):
        cid = self._create(cue="复习卡")
        self.assertIn(cid, [c["id"] for c in cards.due_cards("math")])
        res = cards.review_card(cid, rating=4)
        self.assertGreaterEqual(res["interval_days"], 1)
        self.assertGreater(res["next_due"], date.today().isoformat())
        # 今天不再到期
        self.assertNotIn(cid, [c["id"] for c in cards.due_cards("math")])
        # 评分日志写入
        from db import rows
        logs = rows("SELECT * FROM card_reviews WHERE card_id = ?", (cid,))
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["rating"], 4)

    def test_review_rating1_resets(self):
        cid = self._create(cue="重置卡")
        cards.review_card(cid, rating=4)
        cards.review_card(cid, rating=4)
        state = cards.row("SELECT repetition, interval_days FROM cards WHERE id = ?", (cid,))
        self.assertGreaterEqual(state["repetition"], 2)
        cards.review_card(cid, rating=1)
        state = cards.row("SELECT repetition, interval_days FROM cards WHERE id = ?", (cid,))
        self.assertEqual(state["repetition"], 0)
        self.assertEqual(state["interval_days"], 1)

    def test_delete_cascades(self):
        cid = self._create(cue="待删卡")
        cards.review_card(cid, rating=3)
        self.assertTrue(cards.delete_card(cid))
        from db import rows
        self.assertEqual(rows("SELECT COUNT(*) AS c FROM card_reviews WHERE card_id = ?", (cid,))[0]["c"], 0)
        self.assertIsNone(cards.row("SELECT id FROM cards WHERE id = ?", (cid,)))

    # ── Phase 2：卡片评分回写图谱掌握度 ──
    def _bind_problem(self, cid, mastery):
        with db.DB_LOCK, db.db() as conn:
            cur = conn.execute(
                "INSERT INTO problems(title, topic, content, mastery, concept_ids, created_at, updated_at, subject) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("题", "x", "内容", mastery, f",{cid},", db.now(), db.now(), "math"))
            return int(cur.lastrowid)

    def test_card_review_writes_back_mastery(self):
        cid = self._create(cue="回写卡")
        before = db.row("SELECT mastery FROM concept_progress WHERE concept_id = ?", (cid,))
        cards.review_card(cid, rating=4)  # 触发 update_progress(force)
        after = db.row("SELECT mastery FROM concept_progress WHERE concept_id = ?", (cid,))
        self.assertGreater(after["mastery"], (before or {}).get("mastery", 0))

    def test_card_blends_with_problems(self):
        cid = [n for n in graph.load_graph("math")["nodes"] if n["level"] >= 1][1]["id"]
        self._bind_problem(cid, mastery=4)  # 题目掌握度 0.8
        graph.update_progress("math", force=True)
        prob_only = db.row("SELECT mastery FROM concept_progress WHERE concept_id = ?", (cid,))["mastery"]
        self.assertGreater(prob_only, 0.0)
        c2 = cards.create_card(None, "math", cid, "融合A", "a", "qa", "manual", "active")
        cards.review_card(c2, rating=1)  # 卡评分 0.25，应把掌握度拉低
        blended = db.row("SELECT mastery FROM concept_progress WHERE concept_id = ?", (cid,))["mastery"]
        self.assertGreater(blended, 0.0)
        self.assertLess(blended, prob_only)

    def test_card_signal_includes_only_reviewed(self):
        cid = [n for n in graph.load_graph("math")["nodes"] if n["level"] >= 1][4]["id"]
        c = cards.create_card(None, "math", cid, "信号卡", "a", "qa", "manual", "active")
        with db.DB_LOCK, db.db() as conn:
            sig = graph.card_mastery_signal(conn, "math")
        self.assertNotIn(cid, sig)  # 未评分 → 不纳入信号，避免拉低熟悉概念
        cards.review_card(c, rating=3)
        with db.DB_LOCK, db.db() as conn:
            sig = graph.card_mastery_signal(conn, "math")
        self.assertIn(cid, sig)     # 评分后 → 纳入双驱动
        self.assertGreater(sig[cid][0], 0)
        self.assertGreaterEqual(sig[cid][1], 1)

    # ── Phase 3：按先修链的学习路径（用独立学科做受控图，避免种子数据干扰）──
    def _add_concept(self, conn, name, subject, parent_id=1, mastery=None):
        cur = conn.execute(
            "INSERT INTO concepts(name, parent_id, chapter_id, difficulty, subject, created_at, source) "
            "VALUES (?, ?, 1, 0.3, ?, ?, 'import')", (name, parent_id, subject, db.now()))
        cid = int(cur.lastrowid)
        if mastery is not None:
            conn.execute("INSERT INTO concept_progress(concept_id, mastery, reviews, updated_at) "
                         "VALUES (?, ?, 0, ?)", (cid, mastery, db.now()))
        return cid

    def test_learning_path(self):
        with db.DB_LOCK, db.db() as conn:
            # C：无先修且弱 → 应被推荐为「现在就学」
            c = self._add_concept(conn, "LP_C", "lptest1", mastery=0.3)
            # A：低掌握的先修（被 B 引用 → 非叶子）；B：被 A 卡住的弱概念
            a = self._add_concept(conn, "LP_A", "lptest1", mastery=0.1)
            b = self._add_concept(conn, "LP_B", "lptest1", parent_id=a, mastery=0.2)
            conn.execute("INSERT INTO concept_links(concept_a, concept_b, relation) VALUES (?, ?, 'prerequisite')", (a, b))
        p = graph.learning_path("lptest1")
        self.assertEqual(p["now"]["name"], "LP_C")
        self.assertEqual(p["now"]["reason"], "ready")
        self.assertIn(c, [w["concept_id"] for w in p["ready_weak"]])
        self.assertEqual(p["stats"]["learned"] + p["stats"]["weak_ready"] + p["stats"]["weak_blocked"],
                         p["stats"]["total"])
        blocked_b = next(x for x in p["blocked"] if x["name"] == "LP_B")
        self.assertIn("LP_A", blocked_b["missing"])

    def test_learning_path_no_ready_prefers_prereq(self):
        with db.DB_LOCK, db.db() as conn:
            a = self._add_concept(conn, "LPQ_A", "lptest2", mastery=0.1)
            b = self._add_concept(conn, "LPQ_B", "lptest2", parent_id=a, mastery=0.2)
            conn.execute("INSERT INTO concept_links(concept_a, concept_b, relation) VALUES (?, ?, 'prerequisite')", (a, b))
        p = graph.learning_path("lptest2")
        # 无任何「可立即学」弱概念 → now 指向最弱被卡概念的未达标先修
        self.assertIsNotNone(p["now"])
        self.assertEqual(p["now"]["reason"], "prerequisite")
        self.assertEqual(p["now"]["name"], "LPQ_A")
        self.assertEqual(p["now"]["for"], "LPQ_B")

    # ── Phase 3：手动画线 link_concepts ──
    def test_link_concepts(self):
        na = graph.add_concept("LKA", 0, subject="math")
        nb = graph.add_concept("LKB", 0, subject="math")
        self.assertTrue(na and nb)
        from db import rows
        ok, err = graph.link_concepts(na, nb, "prerequisite", "math")
        self.assertTrue(ok, err)
        # 幂等：重复连线不产生重复边
        self.assertTrue(graph.link_concepts(na, nb, "prerequisite", "math")[0])
        self.assertEqual(len(rows("SELECT 1 FROM concept_links WHERE concept_a=? AND concept_b=? AND relation=?",
                                  (na, nb, "prerequisite"))), 1)
        # 对称关系规整：反向连相关仍同一条边
        self.assertTrue(graph.link_concepts(nb, na, "related", "math")[0])
        self.assertEqual(len(rows("SELECT 1 FROM concept_links WHERE relation='related' "
                                  "AND ((concept_a=? AND concept_b=?) OR (concept_a=? AND concept_b=?))",
                                  (min(na, nb), max(na, nb), max(na, nb), min(na, nb)))), 1)
        # 自环 / 非法关系 / 跨学科
        self.assertFalse(graph.link_concepts(na, na, "related", "math")[0])
        self.assertFalse(graph.link_concepts(na, nb, "weird", "math")[0])
        np_ = graph.add_concept("LKC", 0, subject="physics")
        self.assertFalse(graph.link_concepts(na, np_, "related", "math")[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)