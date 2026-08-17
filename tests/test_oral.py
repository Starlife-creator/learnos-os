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


if __name__ == "__main__":
    unittest.main()
