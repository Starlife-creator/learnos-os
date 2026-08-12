"""B4 真题对齐 + 考试就绪度测试。"""
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
import exam
from handler import Handler

_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)


class TestExam(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="exam_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls.temp_dir.name) / "exam_test.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()
        # 造两个考点错题：牛顿第二定律 mastery=4，动量守恒 mastery=1
        with db.db() as conn:
            from db import now
            for title, topic, mastery in (("牛二错题", "牛顿第二定律", 4), ("动量错题", "动量守恒", 1)):
                conn.execute(
                    "INSERT INTO problems(title, course, topic, content, mastery, created_at, updated_at) "
                    "VALUES (?, '力学', ?, '内容', ?, ?, ?)",
                    (title, topic, mastery, now(), now()),
                )
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db
        cls.temp_dir.cleanup()

    def request(self, path, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "X-Requested-With": "PhysicsStudyOS"},
        )
        with urllib.request.urlopen(req, timeout=8) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_create_paper_and_questions(self):
        status, r = self.request("/api/exam/papers", "POST", {
            "name": "2026 期末力学卷", "exam_date": "2026-06-01", "target": 80,
        })
        self.assertEqual(status, 201)
        pid = r["id"]
        status, r = self.request(f"/api/exam/papers/{pid}/questions", "POST", {
            "questions": [
                {"qno": "1", "topic": "牛顿第二定律", "weight": 2},
                {"qno": "2", "topic": "动量守恒", "weight": 1},
                {"qno": "3", "topic": "动量守恒", "weight": 1, "related_problems": [1]},
                {"qno": "4", "topic": "", "weight": 1},
                {"qno": "5", "topic": "不存在考点", "weight": 1},
            ],
        })
        self.assertEqual(status, 201)
        self.assertEqual(r["added"], 4)  # 空 topic 跳过

    def test_readiness_math(self):
        pid = exam.create_paper("就绪度卷", "2026-01-01", 80)
        exam.add_questions(pid, [
            {"qno": "1", "topic": "牛顿第二定律", "weight": 2},
            {"qno": "2", "topic": "动量守恒", "weight": 1},
        ])
        data = exam.paper_readiness(pid)
        # 牛二 mastery 4/5=0.8 ×2 + 动量 1/5=0.2 ×1，总权重 3 → (1.6+0.2)/3=60%
        self.assertEqual(data["readiness"], 60.0)
        self.assertEqual(data["gap_to_target"], 20.0)
        self.assertEqual(data["hit_rate"], 100.0)  # 两考点都有错题覆盖
        self.assertIn("动量守恒", data["gaps"])

    def test_overall_and_delete(self):
        pid = exam.create_paper("空卷", "", 70)
        status, r = self.request("/api/exam/papers")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(r["papers"]), 1)
        self.assertIsNotNone(r["overall"])
        status, r = self.request(f"/api/exam/papers/{pid}", "DELETE")
        self.assertEqual(status, 200)
        status, r = self.request("/api/exam/papers")
        self.assertFalse(any(p["paper"]["id"] == pid for p in r["papers"]))
        self.assertIsNotNone(r["overall"])  # 其他测试创建的卷仍在

    def test_related_problems_link(self):
        pid = exam.create_paper("互链卷")
        exam.add_questions(pid, [{"qno": "1", "topic": "动量守恒", "related_problems": ["7"]}])
        data = exam.paper_readiness(pid)
        self.assertEqual(data["questions"][0]["related_problems"], [7])


if __name__ == "__main__":
    unittest.main()
