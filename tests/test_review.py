"""测试 SM-2 间隔复习算法。"""
import json
import sys
import tempfile
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
import db
from handler import Handler
from review import compute_review, clamp_mastery, ReviewResult


class TestClampMastery(unittest.TestCase):
    def test_below_min(self):
        self.assertEqual(clamp_mastery(0), 1)
        self.assertEqual(clamp_mastery(-5), 1)

    def test_above_max(self):
        self.assertEqual(clamp_mastery(6), 5)
        self.assertEqual(clamp_mastery(99), 5)

    def test_in_range(self):
        self.assertEqual(clamp_mastery(3), 3)


class TestComputeReview(unittest.TestCase):
    def test_rating_1_resets(self):
        """完全忘记：间隔重置为1，重复次数归零。"""
        r = compute_review(rating=1, prev_interval=10, prev_ease=2.5, prev_repetition=3)
        self.assertEqual(r.interval_days, 1)
        self.assertEqual(r.repetition, 0)
        self.assertEqual(r.mastery, 1)

    def test_rating_2_first_time(self):
        """模糊：第一次记住，间隔=1。"""
        r = compute_review(rating=2, prev_interval=1, prev_ease=2.5, prev_repetition=0)
        self.assertEqual(r.repetition, 1)
        self.assertEqual(r.interval_days, 1)

    def test_rating_3_second_time(self):
        """基本正确：第二次记住，间隔=3。"""
        r = compute_review(rating=3, prev_interval=1, prev_ease=2.5, prev_repetition=1)
        self.assertEqual(r.repetition, 2)
        self.assertEqual(r.interval_days, 3)

    def test_rating_4_third_time_uses_ease(self):
        """完全掌握：第三次记住，间隔=上次间隔*ease_factor。"""
        r = compute_review(rating=4, prev_interval=3, prev_ease=2.5, prev_repetition=2)
        self.assertEqual(r.repetition, 3)
        self.assertEqual(r.interval_days, round(3 * r.ease_factor))

    def test_ease_factor_decreases_on_bad_rating(self):
        """低评分应降低 ease_factor。"""
        r = compute_review(rating=1, prev_interval=5, prev_ease=2.5, prev_repetition=2)
        self.assertLess(r.ease_factor, 2.5)

    def test_ease_factor_never_below_min(self):
        """ease_factor 不应低于 1.3。"""
        r = compute_review(rating=1, prev_interval=5, prev_ease=1.3, prev_repetition=2)
        self.assertGreaterEqual(r.ease_factor, 1.3)

    def test_ease_factor_increases_on_good_rating(self):
        """高评分应提高 ease_factor。"""
        r = compute_review(rating=4, prev_interval=3, prev_ease=2.5, prev_repetition=2)
        self.assertGreaterEqual(r.ease_factor, 2.5)

    def test_rating_clamped(self):
        """超出范围的 rating 应被夹紧。"""
        r1 = compute_review(rating=0, prev_interval=1, prev_ease=2.5, prev_repetition=0)
        r2 = compute_review(rating=99, prev_interval=1, prev_ease=2.5, prev_repetition=0)
        self.assertEqual(r1.interval_days, 1)  # clamped to 1 = forgot
        self.assertGreater(r2.interval_days, 0)  # clamped to 4 = mastered

    def test_interval_at_least_1(self):
        """间隔天数至少为1。"""
        r = compute_review(rating=4, prev_interval=1, prev_ease=1.3, prev_repetition=0)
        self.assertGreaterEqual(r.interval_days, 1)


class TestQueueReadOnly(unittest.TestCase):
    """F1 守恒条款锁定：复习/卡片队列拉取必须纯读——读取前后库内零变化。

    任何新增的队列排序/过滤/惩罚逻辑（如 M2 错因加权）都不得在出队路径写库。
    """

    server: ThreadingHTTPServer
    thread: Thread

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(
            prefix="review_ro_", dir=Path(__file__).resolve().parent / ".tmp")
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "review_ro_test.db"
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
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = json.dumps(body) if body else None
        headers = {"Content-Type": "application/json", "X-Requested-With": "LearnOS"}
        conn.request(method, path, data, headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        conn.close()
        return resp.status, json.loads(raw)

    def _snapshot(self) -> dict:
        """全表快照：行数 + 每行完整内容（防任何隐式写）。"""
        out = {}
        for table in ("problems", "reviews", "cards", "card_reviews",
                      "concepts", "concept_progress", "mastery_log"):
            rows = db.rows(f"SELECT * FROM {table}")
            out[table] = [dict(r) for r in rows]
        return out

    def test_review_and_card_queues_are_readonly(self):
        # 造数：2 道题（建题自动排复习任务）+ 2 张到期卡
        for i, title in enumerate(("力学守恒题", "电磁感应题"), start=1):
            status, data = self._request("POST", "/api/problems", {
                "title": title, "course": "测试课程", "topic": "测试主题",
                "content": f"第{i}题内容：求物理量。",
            })
            self.assertEqual(status, 201, data)
        for i in range(2):
            status, data = self._request("POST", "/api/cards", {
                "cue": f"测试卡{i}的提示", "answer": f"测试卡{i}的答案",
            })
            self.assertEqual(status, 200, data)

        before = self._snapshot()
        # 两条出队路径各拉取一次
        status, rv = self._request("GET", "/api/reviews")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(rv["items"]), 2, "复习队列应含新建题")
        status, cards_due = self._request("GET", "/api/cards/due")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(cards_due["items"]), 2, "卡片队列应含新卡")
        after = self._snapshot()
        # 守恒断言：拉取前后所有相关表逐行一致（零写入）
        for table in before:
            self.assertEqual(before[table], after[table],
                             f"拉取队列后 {table} 表发生变化（违反 F1 纯读条款）")


if __name__ == "__main__":
    unittest.main()
