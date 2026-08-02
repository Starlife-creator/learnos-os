"""测试 SM-2 间隔复习算法。"""
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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


if __name__ == "__main__":
    unittest.main()
