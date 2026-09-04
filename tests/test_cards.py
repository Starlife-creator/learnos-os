"""v25 概念闪卡测试：卡片 CRUD、草稿生成（离线）、FSRS 调度、级联删除。"""
import json
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

    # ── C2：语义契约（先查后写 + 泄题检查）──

    def test_leaks_answer_detection(self):
        """C2：正面含完整答案（含大小写/空白变体）判泄题；短答案不误伤。"""
        # 正面直接含完整答案（原样 / 大小写 / 空白差异）→ 泄题
        self.assertTrue(cards.leaks_answer("什么是 F=ma？公式 F=ma", "F=ma"))
        self.assertTrue(cards.leaks_answer("牛顿第二定律的内容是：力与加速度成正比", "力与加速度成正比"))
        self.assertTrue(cards.leaks_answer("Answer: THE ANSWER IS Gravity", "the answer is gravity"))
        self.assertTrue(cards.leaks_answer("填空：万 有 引 力", "万有引力"))
        # 反向（answer 比 cue 长）不算泄题
        self.assertFalse(cards.leaks_answer("什么是 F=ma？", "F=ma 是牛顿第二定律的定量表达"))
        # 正常卡不误伤
        self.assertFalse(cards.leaks_answer("用自己的话解释「万有引力」", "万有引力是物体间的相互吸引力"))
        # 答案过短（<4 字符）不判泄题
        self.assertFalse(cards.leaks_answer("力的单位是什么？答案是牛顿", "牛顿"))

    def test_create_card_duplicate_cue_blocked(self):
        """C2：cue 仅大小写/空白差异的卡被拦截（应用层归一化，Unicode casefold）。"""
        a = self._create(cue="  唯一判重 Cue  ", answer="答案一")
        # 完全相同 → 拦截
        with self.assertRaises(ValueError):
            self._create(cue="唯一判重 Cue", answer="答案二")
        # 仅大小写差异 → 拦截
        with self.assertRaises(ValueError):
            self._create(cue="唯一判重 CUE", answer="答案三")
        # 仅空白差异 → 拦截
        with self.assertRaises(ValueError):
            self._create(cue=" 唯一判重   Cue ", answer="答案四")
        # 不同 cue → 正常创建
        self.assertTrue(self._create(cue="另一张不同的卡", answer="答案五"))
        # 编辑自身（card_id 覆盖）不判重复
        cards.create_card(a, "math", self.concept_id, "唯一判重 Cue", "编辑后的答案",
                          "qa", "manual", "active")

    def test_create_card_answer_leak_blocked(self):
        """C2：正面含完整答案的卡被拒收（要求改写正面）。"""
        with self.assertRaises(ValueError):
            self._create(cue="什么是牛顿第二定律？答：物体加速度与合力成正比",
                         answer="物体加速度与合力成正比")
        # 改写正面（不含完整答案）后放行
        self.assertTrue(self._create(cue="什么是牛顿第二定律？", answer="物体加速度与合力成正比"))

    def test_generate_drafts_filters_leak_and_duplicate(self):
        """C2：AI 草稿泄题/与已有卡重复 → 回炉丢弃；全滤光回退离线。"""
        from unittest.mock import patch
        # 已有一张卡，AI 又出了张仅大小写不同的重复卡 + 一张泄题卡 + 一张干净卡
        self._create(cue="已有概念卡 QA", answer="已有答案")
        fake = {"cards": [
            {"cue": "已有概念卡 qa", "answer": "重复卡", "kind": "qa"},          # 重复 → 丢弃
            {"cue": "什么是X？X的定义是冒号后这句", "answer": "X的定义是冒号后这句", "kind": "qa"},  # 泄题 → 丢弃
            {"cue": "干净草稿：解释X的适用条件", "answer": "X在惯性系中成立", "kind": "qa"},          # 保留
        ]}
        with patch("ai.ai_configured", return_value=True), \
             patch("ai.call_ai", return_value=json.dumps(fake, ensure_ascii=False)):
            drafts = cards.generate_drafts("math", self.concept_id, use_ai=True)
        cues = [d["cue"] for d in drafts]
        self.assertEqual(cues, ["干净草稿：解释X的适用条件"], "泄题与重复草稿应被回炉丢弃")
        # 全部泄题/重复 → 回退离线草稿（模板 cue 不含答案，天然不泄题）
        fake2 = {"cards": [
            {"cue": "已有概念卡 QA", "answer": "重复", "kind": "qa"},
            {"cue": "什么是Y？Y的定义", "answer": "Y的定义", "kind": "qa"},
        ]}
        with patch("ai.ai_configured", return_value=True), \
             patch("ai.call_ai", return_value=json.dumps(fake2, ensure_ascii=False)):
            drafts2 = cards.generate_drafts("math", self.concept_id, use_ai=True)
        self.assertTrue(drafts2)
        for d in drafts2:
            self.assertFalse(cards.leaks_answer(d["cue"], d["answer"]))

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

    # ── D1：评分日志前后快照 ──
    _SNAP_FIELDS = ("due_date", "interval_days", "ease_factor", "repetition",
                    "state", "stability", "difficulty", "last_review")

    def _card_state(self, cid):
        c = cards.row("SELECT * FROM cards WHERE id = ?", (cid,))
        return {f: c[f] for f in self._SNAP_FIELDS}

    def test_review_logs_prev_cur_snapshot(self):
        """D1：评分行携带 prev 全字段快照与 cur FSRS 三态，且与 cards 行一致。"""
        cid = self._create(cue="快照卡")
        # 先复习一次，让卡片带上非默认状态（有 prev 可拍）
        cards.review_card(cid, rating=3)
        before = self._card_state(cid)
        cards.review_card(cid, rating=4)
        after = self._card_state(cid)
        log = cards.row(
            "SELECT * FROM card_reviews WHERE card_id = ? AND undone = 0 "
            "ORDER BY id DESC LIMIT 1", (cid,))
        # prev 快照 = 评分前 cards 行（逐字段）
        self.assertEqual(log["prev_due"], before["due_date"])
        self.assertEqual(log["prev_interval"], before["interval_days"])
        self.assertAlmostEqual(log["prev_ease"], before["ease_factor"])
        self.assertEqual(log["prev_repetition"], before["repetition"])
        self.assertEqual(log["prev_state"], before["state"])
        self.assertAlmostEqual(log["prev_stability"], before["stability"])
        self.assertAlmostEqual(log["prev_difficulty"], before["difficulty"])
        self.assertEqual(log["prev_last_review"], before["last_review"])
        # cur 侧（due_date/interval 复用既有列）与评分后 cards 行一致
        self.assertEqual(log["due_date"], after["due_date"])
        self.assertEqual(log["interval_days"], after["interval_days"])
        self.assertEqual(log["cur_state"], after["state"])
        self.assertAlmostEqual(log["cur_stability"], after["stability"])
        self.assertAlmostEqual(log["cur_difficulty"], after["difficulty"])

    # ── D2：撤销评分 ──
    def test_undo_review_restores_exactly(self):
        """D2：撤销后 cards 行与评分前逐字段一致；队列重现该卡；重复撤销报错。"""
        cid = self._create(cue="撤销卡")
        before = self._card_state(cid)
        cards.review_card(cid, rating=4)
        self.assertNotIn(cid, [c["id"] for c in cards.due_cards("math")])
        res = cards.undo_review(cid)
        self.assertEqual(res["restored_due"], before["due_date"])
        after = self._card_state(cid)
        for f in self._SNAP_FIELDS:
            self.assertEqual(after[f], before[f], f"撤销后字段 {f} 未恢复")
        # 队列重现该卡（due 恢复为当日）
        self.assertIn(cid, [c["id"] for c in cards.due_cards("math")])
        # 无行可撤 → 显式报错（连撤不越界）
        with self.assertRaises(ValueError):
            cards.undo_review(cid)

    def test_undo_review_chain_and_mastery(self):
        """D2：连撤两次逐层恢复；已作废评分不再计入卡驱动掌握度信号。"""
        cid = self._create(cue="连撤卡")
        s0 = self._card_state(cid)
        cards.review_card(cid, rating=4)
        cards.review_card(cid, rating=3)
        # 连撤两次 → 回到最初状态
        cards.undo_review(cid)
        cards.undo_review(cid)
        for f in self._SNAP_FIELDS:
            self.assertEqual(self._card_state(cid)[f], s0[f], f"连撤后字段 {f} 未回到初始")
        undone = cards.rows("SELECT undone FROM card_reviews WHERE card_id = ?", (cid,))
        self.assertTrue(all(u["undone"] == 1 for u in undone), "两次撤销都应作废日志行")
        # 作废行不进掌握度信号：独立概念上评分→撤销，信号不应包含该概念
        with db.DB_LOCK, db.db() as conn:
            nc = conn.execute(
                "INSERT INTO concepts(name, parent_id, chapter_id, difficulty, subject, created_at, source) "
                "VALUES ('撤销信号概念', 0, 1, 0.3, 'math', ?, 'import')", (db.now(),)).lastrowid
        sc = cards.create_card(None, "math", nc, "信号撤销卡", "a", "qa", "manual", "active")
        cards.review_card(sc, rating=4)
        with db.DB_LOCK, db.db() as conn:
            self.assertIn(nc, graph.card_mastery_signal(conn, "math"), "评分后应纳入信号")
        cards.undo_review(sc)
        with db.DB_LOCK, db.db() as conn:
            self.assertNotIn(nc, graph.card_mastery_signal(conn, "math"),
                             "已作废评分不得计入卡驱动掌握度")

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

    def test_delete_card_trash_restore(self):
        """D3：卡片删除（级联评分日志）入回收站，恢复后卡片与日志原样回来。"""
        import trash
        cid = self._create(cue="回收站卡")
        cards.review_card(cid, rating=3)
        self.assertTrue(cards.delete_card(cid))
        self.assertEqual(cards.rows(
            "SELECT COUNT(*) AS c FROM card_reviews WHERE card_id = ?", (cid,))[0]["c"], 0)
        t = trash.restore("card", cid)
        self.assertIsNotNone(t)
        card = cards.row("SELECT * FROM cards WHERE id = ?", (cid,))
        self.assertEqual(card["cue"], "回收站卡")
        self.assertEqual(card["repetition"], 1, "评分后的调度状态应随快照恢复")
        logs = cards.rows("SELECT * FROM card_reviews WHERE card_id = ?", (cid,))
        self.assertEqual(len(logs), 1, "评分日志应随快照恢复")
        self.assertEqual(logs[0]["rating"], 3)
        # 恢复的卡片回到到期队列（due 仍是评分推后的日期）
        self.assertEqual(cards.row("SELECT due_date FROM cards WHERE id = ?", (cid,))["due_date"],
                         logs[0]["due_date"])

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
        for _ in range(3):  # 3 份题目证据：越过 M1 封顶线，验证纯双驱动融合
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

    # ── Phase 3：手动画线 link_concepts（G1：reason 必填） ──
    def test_link_concepts(self):
        na = graph.add_concept("LKA", 0, subject="math")
        nb = graph.add_concept("LKB", 0, subject="math")
        self.assertTrue(na and nb)
        from db import rows
        ok, err = graph.link_concepts(na, nb, "prerequisite", "math", reason="LKB 依赖 LKA 的定义")
        self.assertTrue(ok, err)
        # G1：溯源列落库（strength 默认 soft，reason 原样保存）
        lk = rows("SELECT strength, reason FROM concept_links WHERE concept_a=? AND concept_b=? AND relation='prerequisite'",
                  (na, nb))[0]
        self.assertEqual(lk["strength"], "soft")
        self.assertEqual(lk["reason"], "LKB 依赖 LKA 的定义")
        # 幂等：重复连线不产生重复边
        self.assertTrue(graph.link_concepts(na, nb, "prerequisite", "math", reason="重复连线")[0])
        self.assertEqual(len(rows("SELECT 1 FROM concept_links WHERE concept_a=? AND concept_b=? AND relation=?",
                                  (na, nb, "prerequisite"))), 1)
        # 对称关系规整：反向连相关仍同一条边
        self.assertTrue(graph.link_concepts(nb, na, "related", "math", reason="同章相关")[0])
        self.assertEqual(len(rows("SELECT 1 FROM concept_links WHERE relation='related' "
                                  "AND ((concept_a=? AND concept_b=?) OR (concept_a=? AND concept_b=?))",
                                  (min(na, nb), max(na, nb), max(na, nb), min(na, nb)))), 1)
        # 自环 / 非法关系 / 跨学科 / G1 缺理由与非法强度
        self.assertFalse(graph.link_concepts(na, na, "related", "math", reason="x")[0])
        self.assertFalse(graph.link_concepts(na, nb, "weird", "math", reason="x")[0])
        np_ = graph.add_concept("LKC", 0, subject="physics")
        self.assertFalse(graph.link_concepts(na, np_, "related", "math", reason="x")[0])
        self.assertFalse(graph.link_concepts(na, nb, "related", "math")[0], "G1：缺 reason 必须拒绝")
        self.assertFalse(graph.link_concepts(na, nb, "related", "math", reason="x", strength="soggy")[0],
                         "G1：strength 仅允许 hard|soft")

    def test_generate_batch_drafts(self):
        """C1 制卡 Pipeline 分层：按概念里程碑清单逐块出卡，单概念失败跳过不中断。"""
        # 两个有效概念 + 一个不存在概念 + 重复 id + 非法 id
        n1 = graph.add_concept("批出卡概念A", 0, subject="math")
        n2 = graph.add_concept("批出卡概念B", 0, subject="math")
        out = cards.generate_batch_drafts(
            "math", [n1, n2, 999999, n1, "bad"], use_ai=False)
        self.assertEqual(len(out["results"]), 2, "两个有效概念各出一组草稿")
        names = {r["concept_name"] for r in out["results"]}
        self.assertEqual(names, {"批出卡概念A", "批出卡概念B"})
        for r in out["results"]:
            self.assertTrue(r["drafts"], "离线模板应产出草稿")
        self.assertEqual(len(out["failed"]), 1, "不存在的概念进 failed")
        self.assertEqual(out["failed"][0]["concept_id"], 999999)
        # 全部无效 → results 空、failed 齐全（handler 据此返回 400）
        out2 = cards.generate_batch_drafts("math", [999998], use_ai=False)
        self.assertEqual(out2["results"], [])
        self.assertEqual(out2["failed"][0]["concept_id"], 999998)


if __name__ == "__main__":
    unittest.main(verbosity=2)