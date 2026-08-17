"""端到端覆盖本轮方案剩余特性（#54-#61）的集成测试。

所有测试使用临时数据库（在导入项目模块前固定 LEARNOS_DB），不污染生产库。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

# 关键：必须在 import 任何项目模块之前把数据库指向临时文件，
# 否则 db.DB_PATH 在 import 时已被固定为真实 learnos.db。
_TMP = tempfile.mkdtemp(prefix="learnos_plan_")
os.environ["LEARNOS_DB"] = str(Path(_TMP) / "test.db")

import config as config_module  # noqa: E402
import db as db_module  # noqa: E402

config_module.DB_PATH = Path(_TMP) / "test.db"
db_module.DB_PATH = config_module.DB_PATH
db_module.init_db()

import social  # noqa: E402
import exam  # noqa: E402
import interop  # noqa: E402
import render_config  # noqa: E402
import agent_rules  # noqa: E402
import plugins  # noqa: E402


def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    # Sandbox safe-delete hook may block unlink; an empty file is still a valid
    # brand-new SQLite db, so tolerating the failure is safe.
    try:
        os.unlink(path)
    except OSError:
        pass
    return path


class TestStudyCheckins(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import social
        cls.social = social
        with db_module.DB_LOCK, db_module.db() as conn:
            conn.execute("DELETE FROM study_checkins")
        # 连续两天打卡（今天 + 昨天）
        social.add_checkin("physics", 30, "复习力学", date.today().isoformat())
        # 昨天
        social.add_checkin("physics", 20, "错题", (date.today() - timedelta(days=1)).isoformat())
        # 另一学科
        social.add_checkin("chemistry", 15, "平衡", date.today().isoformat())

    def test_streak_today_and_yesterday(self):
        self.assertEqual(self.social.streak("physics"), 2)
        self.assertEqual(self.social.streak("chemistry"), 1)
        self.assertEqual(self.social.streak(None), 2)

    def test_total_minutes(self):
        self.assertEqual(self.social.total_minutes("physics"), 50)
        self.assertEqual(self.social.total_minutes(None), 65)

    def test_export_social_no_answers(self):
        payload = self.social.export_social("physics")
        self.assertIn("streak_days", payload)
        self.assertIn("weak_topics", payload)
        # 关键隐私约束：不得含任何题目内容/答案字段
        for banned in ("content", "my_attempt", "answer", "hint"):
            self.assertNotIn(banned, json.dumps(payload, ensure_ascii=False).lower())


class TestExamPrediction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import exam
        cls.exam = exam
        with db_module.DB_LOCK, db_module.db() as conn:
            conn.execute("DELETE FROM exam_questions")
            conn.execute("DELETE FROM exam_papers")
        pid = exam.create_paper("一模", "2026-09-01", 85)
        exam.add_questions(pid, [
            {"qno": "1", "topic": "牛顿定律", "weight": 2, "content": "q1"},
            {"qno": "2", "topic": "能量", "weight": 1, "content": "q2"},
        ])
        cls.pid = pid

    def test_readiness_exists(self):
        r = self.exam.paper_readiness(self.pid)
        self.assertEqual(r["question_count"], 2)
        self.assertIn("readiness", r)

    def test_predict_score_shape(self):
        p = self.exam.predict_score(self.pid)
        self.assertIn("predicted", p)
        self.assertIn("lower", p)
        self.assertIn("upper", p)
        self.assertLessEqual(p["lower"], p["predicted"])
        self.assertGreaterEqual(p["upper"], p["predicted"])
        self.assertIn(p["confidence"], ("insufficient", "low", "medium", "high"))

    def test_predict_missing_paper(self):
        self.assertEqual(self.exam.predict_score(99999), {})


class TestInteropExport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import interop
        cls.interop = interop
        with db_module.DB_LOCK, db_module.db() as conn:
            conn.execute("DELETE FROM reviews")
            conn.execute("DELETE FROM problems")
            conn.execute(
                "INSERT INTO problems(title, course, topic, content, my_attempt, error_type, mastery, "
                "created_at, updated_at, subject) VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("题1", "高一", "力学", "题干", "我的作答", "概念不清", 3, "2026-01-01", "2026-01-01", "physics"),
            )

    def test_csv_bom_and_columns(self):
        out = self.interop.export_csv("physics", include_answers=True)
        self.assertTrue(out.startswith("\ufeff"))
        self.assertIn("content", out)
        self.assertIn("我的作答", out)  # 含答案
        no_answer = self.interop.export_csv("physics", include_answers=False)
        self.assertNotIn("我的作答", no_answer)

    def test_md_hides_answers(self):
        md = self.interop.export_md("physics", include_answers=False)
        self.assertIn("# LearnOS", md)
        self.assertNotIn("我的作答", md)
        md2 = self.interop.export_md("physics", include_answers=True)
        self.assertIn("我的作答", md2)


class TestRenderConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import render_config
        cls.rc = render_config
        with db_module.DB_LOCK, db_module.db() as conn:
            conn.execute("DELETE FROM settings WHERE key LIKE 'render_config_%'")

    def test_default_merge_builtin(self):
        cfg = self.rc.get_render_config("physics")
        self.assertEqual(cfg["formula_engine"], "latex")
        self.assertEqual(cfg["display_name"], "物理")

    def test_set_and_get(self):
        merged = self.rc.set_render_config("physics", {"accent": "#ff0000", "sig_figs": 2})
        self.assertEqual(merged["accent"], "#ff0000")
        again = self.rc.get_render_config("physics")
        self.assertEqual(again["accent"], "#ff0000")
        self.assertEqual(again["sig_figs"], 2)

    def test_rejects_unknown_key(self):
        merged = self.rc.set_render_config("math", {"bogus": "x"})
        self.assertNotIn("bogus", merged)


class TestAgentRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import agent_rules
        cls.ar = agent_rules
        with db_module.DB_LOCK, db_module.db() as conn:
            conn.execute("DELETE FROM reviews")
            conn.execute("DELETE FROM problems")

    def test_cold_start_when_empty(self):
        plan = self.ar.orchestrate("physics")
        ids = [s["id"] for s in plan["suggestions"]]
        self.assertIn("cold_start", ids)

    def test_due_rule_fires(self):
        # 插入到期复习
        with db_module.DB_LOCK, db_module.db() as conn:
            cur = conn.execute(
                "INSERT INTO problems(title, course, topic, content, mastery, created_at, updated_at, subject) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("t", "c", "力学", "x", 3, "2026-01-01", "2026-01-01", "physics"),
            )
            pid = cur.lastrowid
            conn.execute(
                "INSERT INTO reviews(problem_id, due_date, completed, created_at) VALUES (?,?,0,?)",
                (pid, date.today().isoformat(), "2026-01-01"),
            )
        plan = self.ar.orchestrate("physics")
        ids = [s["id"] for s in plan["suggestions"]]
        self.assertIn("review_due", ids)
        # 优先级降序：review_due(P_HIGH/P_CRIT) 应排在最前或靠前
        self.assertGreaterEqual(plan["suggestions"][0]["priority"], 80)

    def test_synthesize_degraded_without_ai(self):
        plan = self.ar.synthesize_plan("physics", use_ai=False)
        self.assertEqual(plan["mode"], "degraded")


class TestPluginsScaffold(unittest.TestCase):
    def test_mcp_registry(self):
        import plugins
        plugins.MCP.register("demo.echo", lambda msg: f"echo:{msg}")
        self.assertIn("demo.echo", plugins.MCP.list_tools())
        self.assertEqual(plugins.MCP.call("demo.echo", {"msg": "hi"}), "echo:hi")
        with self.assertRaises(KeyError):
            plugins.MCP.call("nope.x")

    def test_plugin_api_sandbox(self):
        import plugins
        api = plugins.PluginAPI("demo")
        api.register_command("ping", lambda: "pong")
        self.assertEqual(api.call_command("ping"), "pong")
        with self.assertRaises(KeyError):
            api.call_command("missing")

    def test_load_plugins_empty_dir(self):
        import plugins
        # 不存在的目录应安全返回空列表
        self.assertEqual(plugins.load_plugins(plugins.DEFAULT_PLUGIN_DIR / "__nope__"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
