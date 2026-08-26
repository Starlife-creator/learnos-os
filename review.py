"""间隔复习算法 — SM-2 (SuperMemo 2) 改进版。

rating 含义:
  1 = 完全忘记
  2 = 模糊/有困难
  3 = 基本正确（含小错）
  4 = 完全掌握

SM-2 quality 映射:
  rating 1 → quality 1
  rating 2 → quality 3
  rating 3 → quality 4
  rating 4 → quality 5
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ReviewResult:
    interval_days: int
    ease_factor: float
    repetition: int
    mastery: int


_RATING_TO_QUALITY = {1: 1, 2: 3, 3: 4, 4: 5}
_MIN_EASE = 1.3
_MIN_MASTERY = 1
_MAX_MASTERY = 5


def clamp_mastery(value: int) -> int:
    return max(_MIN_MASTERY, min(_MAX_MASTERY, value))


def compute_review(
    rating: int,
    prev_interval: int,
    prev_ease: float,
    prev_repetition: int,
) -> ReviewResult:
    """根据评分和之前的复习状态，计算下一次复习的间隔、ease_factor 和重复次数。"""
    rating = max(1, min(4, rating))
    quality = _RATING_TO_QUALITY[rating]

    # 更新 ease_factor
    delta = 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)
    new_ease = round(max(_MIN_EASE, prev_ease + delta), 2)

    if quality < 3:
        # 答错/忘记：重置
        new_interval = 1
        new_repetition = 0
        mastery = clamp_mastery(rating)
    else:
        new_repetition = prev_repetition + 1
        if new_repetition == 1:
            new_interval = 1
        elif new_repetition == 2:
            new_interval = 3
        else:
            new_interval = round(prev_interval * new_ease)
        # mastery 随重复次数和评分增长
        mastery = clamp_mastery(prev_repetition + rating)

    return ReviewResult(
        interval_days=max(1, min(new_interval, 730)),  # 工程上限两年：防异常大值按 ease 指数放大永不收敛
        ease_factor=new_ease,
        repetition=new_repetition,
        mastery=mastery,
    )
