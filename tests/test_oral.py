"""F1 苏格拉底引擎测试：状态机推进、级别校准、画像回写、草稿卡（R3）。"""
import json
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
import profile
import graph
from oral import (
    _assess, _next_stage, _detect_weak_points, start_oral, continue_oral, draft_oral_card,
    start_feynman, feynman_self_review, save_feynman_self_review,
)

_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)


class TestSocraticEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="oral_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "oral_test.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db
        try:
            cls._tmp.cleanup()
        except OSError:
            # 沙箱安全删除机制在无回收站时拒绝 rmtree，忽略（临时文件留在 tests/.tmp）
            pass

    def test_assess_levels(self):
        self.assertEqual(_assess("懂了"), 1)
        self.assertEqual(_assess("条件满足时成立，且当参数取特定值时可用，推导过程见 F=ma。"), 3)
        self.assertLessEqual(_assess("x" * 80), 3)

    def test_next_stage_progression(self):
        self.assertEqual(_next_stage(0, 3), (1, 1))
        self.assertEqual(_next_stage(0, 2), (0, 2))
        self.assertEqual(_next_stage(4, 3), (4, 3))

    def test_full_local_session_flow(self):
        sid, q1 = start_oral("牛顿第二定律")
        self.assertTrue(q1)
        session = db.row("SELECT * FROM oral_sessions WHERE id = ?", (sid,))
        state = json.loads(json.loads(session["transcript"])[0]["content"][len("__oral_state__"):])
        self.assertEqual(state["stage"], 0)
        # 5 轮回答（第 1 轮扎实推进，其余薄弱加深，本地模板兜底）
        answers = [
            "牛顿第二定律说明力是改变运动状态的原因，F=ma 是定量表达，当物体质量不变时加速度与合力成正比，前提是惯性系且质量守恒。",
            "浅",
            "浅",
            "浅",
            "浅",
        ]
        finished = False
        for i, ans in enumerate(answers):
            session = db.row("SELECT * FROM oral_sessions WHERE id = ?", (sid,))
            reply = continue_oral(session, ans)
            if "【口试结束】" in reply:
                finished = True
        self.assertTrue(finished)
        # 状态机推进过至少一个阶段（第 1 轮 level 3 触发推进）
        final = db.row("SELECT * FROM oral_sessions WHERE id = ?", (sid,))
        state = json.loads(json.loads(final["transcript"])[0]["content"][len("__oral_state__"):])
        self.assertGreaterEqual(state["stage"], 1)
        # 画像回写：薄弱点追加到 note
        note = profile._get_all()["note"]
        self.assertIn("口试", note)
        self.assertIn("牛顿第二定律", note)

    def test_draft_card_no_persistence(self):
        sid, _ = start_oral("动量守恒")
        for _ in range(5):
            session = db.row("SELECT * FROM oral_sessions WHERE id = ?", (sid,))
            continue_oral(session, "浅")
        session = db.row("SELECT * FROM oral_sessions WHERE id = ?", (sid,))
        draft = draft_oral_card(session)
        self.assertIn("title", draft)
        self.assertIn("content", draft)
        self.assertIn("动量守恒", draft["topic"])
        self.assertIsInstance(draft["tags"], list)
        self.assertTrue(draft["tags"])
        # 草稿不落库：problems 表无新增
        count = db.row("SELECT COUNT(*) AS c FROM problems")["c"]
        self.assertEqual(count, 0)

    def test_detect_weak_points(self):
        t = [
            {"role": "user", "content": "我忘了公式适用前提，算错了。"},
            {"role": "user", "content": "反例没想出来。"},
        ]
        weak = _detect_weak_points(t)
        self.assertIn("适用条件/前提交代不全", weak)
        self.assertIn("概念边界与反例辨析不足", weak)

    def _make_problem(self):
        from db import now
        with db.db() as conn:
            cur = conn.execute(
                "INSERT INTO problems(title, content, topic, course, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("牛顿第二定律错题", "F=ma 中力与加速度的关系？", "牛顿定律", "力学", now(), now()),
            )
        return int(cur.lastrowid)

    def test_feynman_flow(self):
        pid = self._make_problem()
        session = db.row("SELECT * FROM problems WHERE id = ?", (pid,))
        sid, q1 = start_feynman(session)
        self.assertTrue(q1)
        s = db.row("SELECT * FROM oral_sessions WHERE id = ?", (sid,))
        self.assertEqual(s["mode"], "feynman")
        self.assertEqual(s["problem_id"], pid)
        # 第 1 轮讲解 → 引导对照自查
        reply = continue_oral(s, "作用力使物体运动状态改变，力越大加速度越大，质量影响加速度大小。")
        self.assertNotIn("【口试结束】", reply)
        self.assertIn("对照", reply)
        # 第 2 轮自查 → 结束
        s = db.row("SELECT * FROM oral_sessions WHERE id = ?", (sid,))
        reply = continue_oral(s, "漏点：没有讲清适用前提与力的合成方向；讲错：把惯性误说成力；讲清：加速度与力成正比。")
        self.assertIn("【口试结束】", reply)
        s = db.row("SELECT * FROM oral_sessions WHERE id = ?", (sid,))
        self.assertEqual(s["status"], "finished")
        # 自评表草稿（R3 不落库）
        draft = feynman_self_review(s)["draft"]
        self.assertIsNotNone(draft)
        s2 = db.row("SELECT * FROM oral_sessions WHERE id = ?", (sid,))
        self.assertEqual(s2["self_review"], "")
        # 确认保存 → 落库
        self.assertTrue(save_feynman_self_review(sid, draft))
        saved = db.row("SELECT self_review FROM oral_sessions WHERE id = ?", (sid,))["self_review"]
        self.assertIn("gaps", saved)
        # 再次读取返回已保存版本
        s = db.row("SELECT * FROM oral_sessions WHERE id = ?", (sid,))
        out = feynman_self_review(s)
        self.assertEqual(out["saved"]["gaps"], draft["gaps"])

    def test_feynman_self_review_empty_rejected(self):
        pid = self._make_problem()
        session = db.row("SELECT * FROM problems WHERE id = ?", (pid,))
        sid, _ = start_feynman(session)
        self.assertFalse(save_feynman_self_review(sid, {"gaps": [], "wrong": [], "clear": []}))

    def test_socratic_mode_default(self):
        sid, _ = start_oral("动能定理")
        s = db.row("SELECT * FROM oral_sessions WHERE id = ?", (sid,))
        self.assertEqual(s["mode"], "socratic")

    def test_assessment_prompt_hint(self):
        """G2：概念配置口试模板后，_assessment_hint 命中并替换 {{name}}；未配置返回空。"""
        from oral import _assessment_hint, _evidence_context
        with db.db() as conn:
            conn.execute(
                "INSERT INTO concepts(name, parent_id, chapter_id, subject, created_at, assessment_prompt) "
                "VALUES ('万有引力定制概念', 0, 0, 'physics', ?, '请说明{{name}}的公式与适用条件')",
                (db.now(),))
        # 命中：{{name}} 占位替换为概念名
        self.assertEqual(_assessment_hint("万有引力定制概念", "physics"),
                         "请说明万有引力定制概念的公式与适用条件")
        # 未配置模板的概念 / 不存在的概念 → 空串（走默认阶段问题）
        self.assertEqual(_assessment_hint("牛顿第二定律", "physics"), "")
        self.assertEqual(_assessment_hint("不存在概念", "physics"), "")
        self.assertEqual(_assessment_hint("任何", ""), "")
        # G2：判据素材（evidence[]）注入 _ai_followup 的素材文本
        with db.db() as conn:
            conn.execute(
                "INSERT INTO concepts(name, parent_id, chapter_id, subject, created_at, evidence) "
                "VALUES ('判据素材概念', 0, 0, 'physics', ?, ?)",
                (db.now(), '["能独立写出 F=ma", "能说明适用条件"]'))
        ctx = _evidence_context("判据素材概念", "physics")
        self.assertIn("达标判据", ctx)
        self.assertIn("能独立写出 F=ma", ctx)
        self.assertIn("能说明适用条件", ctx)
        # 未配置 / 非法 JSON / 学科为空 → 空串（静默降级）
        self.assertEqual(_evidence_context("牛顿第二定律", "physics"), "")
        self.assertEqual(_evidence_context("判据素材概念", ""), "")
        with db.db() as conn:
            conn.execute(
                "INSERT INTO concepts(name, parent_id, chapter_id, subject, created_at, evidence) "
                "VALUES ('坏判据概念', 0, 0, 'physics', ?, 'not-json')",
                (db.now(),))
        self.assertEqual(_evidence_context("坏判据概念", "physics"), "")


class TestOralSubjectAwareness(unittest.TestCase):
    """v1 口试/费曼学科感知：非物理学科不出现物理专属措辞，physics 保留原行为。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="oral_subj_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "oral_subj_test.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db
        try:
            cls._tmp.cleanup()
        except OSError:
            # 沙箱安全删除机制在无回收站时拒绝 rmtree，忽略（临时文件留在 tests/.tmp）
            pass

    def test_math_oral_no_physics_framing(self):
        sid, q = start_oral("线性代数", "math")
        self.assertNotIn("物理图像", q)
        self.assertNotIn("物理口试", q)

    def test_physics_oral_retains_physics_framing(self):
        sid, q = start_oral("牛顿第二定律", "physics")
        self.assertIn("物理", q)

    def test_math_draft_card_no_physics(self):
        sid, _ = start_oral("微积分", "math")
        for _ in range(5):
            session = db.row("SELECT * FROM oral_sessions WHERE id = ?", (sid,))
            continue_oral(session, "浅", "math")
        session = db.row("SELECT * FROM oral_sessions WHERE id = ?", (sid,))
        draft = draft_oral_card(session)
        self.assertNotIn("物理图像", draft["content"])


class TestOralMasteryEvent(unittest.TestCase):
    """D4：口试完成时落 mastery_events 一行（entry_point='oral'）—— 审计/回放留痕。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="oral_evt_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "oral_evt.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()
        # 注册一个概念节点让 topic 能映射到 concept_id（record_audit_event 按 name 查）
        with db.db() as conn:
            conn.execute(
                "INSERT INTO concepts(name, parent_id, chapter_id, subject, created_at) "
                "VALUES ('牛顿第二定律', 0, 0, 'physics', ?)",
                (db.now(),))

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db
        try:
            cls._tmp.cleanup()
        except OSError:
            pass

    def test_completed_oral_emits_audit_event(self):
        sid, _ = start_oral("牛顿第二定律", "physics")
        # 跑满 5 轮触发 _write_back_profile（口试走 finish 分支）
        for _ in range(5):
            session = db.row("SELECT * FROM oral_sessions WHERE id = ?", (sid,))
            continue_oral(session, "浅答", "physics")
        evs = db.rows(
            "SELECT entry_point, evidence, prev_mastery, cur_mastery FROM mastery_events "
            "WHERE entry_point = 'oral' ORDER BY id"
        )
        self.assertEqual(len(evs), 1, f"应落 1 行 oral 事件，实际 {len(evs)}")
        self.assertEqual(evs[0]["entry_point"], "oral")
        self.assertIn("薄弱点", evs[0]["evidence"])
        self.assertEqual(evs[0]["prev_mastery"], evs[0]["cur_mastery"],
                         "audit-only 事件 prev=cur（口试不直接改 mastery）")

    def test_unknown_topic_returns_zero(self):
        # topic 在 concepts 表里查不到 → 优雅返回 0，不抛错
        n = graph.record_audit_event("physics", "完全不存在的概念", "oral", "evidence")
        self.assertEqual(n, 0)

    def test_empty_topic_returns_zero(self):
        self.assertEqual(graph.record_audit_event("physics", "", "oral", ""), 0)


if __name__ == "__main__":
    unittest.main()
