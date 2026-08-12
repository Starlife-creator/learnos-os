"""FSRS-6 调度适配层（A1）。

规则（R2）：vendored 依赖缺失时必须降级，主路径仅标准库。
- vendor/fsrs 目录存在 → 使用 FSRS-6 调度；
- 缺失/导入失败 → 回退 SM-2（review.compute_review），功能不中断。

复习评分（rating 1-4）映射 FSRS Rating：
  1=Again 2=Hard 3=Good 4=Easy

状态持久化：problems.state/stability/difficulty 列（v6 迁移）。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from config import LOG

_VENDOR_DIR = Path(__file__).resolve().parent / "vendor" / "fsrs"

try:
    if _VENDOR_DIR.exists():
        sys.path.insert(0, str(_VENDOR_DIR))
    from fsrs import Scheduler, Card, Rating  # type: ignore
    _FSRS_AVAILABLE = True
except Exception as exc:  # pragma: no cover - 降级路径
    _FSRS_AVAILABLE = False
    LOG.warning("FSRS vendored 依赖不可用，回退 SM-2: %s", exc)


@dataclass
class FsrsState:
    """可持久化的 FSRS 卡状态（对应 problems 表列）。"""
    state: int
    stability: float
    difficulty: float
    due: str
    last_review: str
    elapsed_days: int
    scheduled_days: int


def fsrs_available() -> bool:
    return _FSRS_AVAILABLE


def _state_to_card(
    state: int,
    stability: float,
    difficulty: float,
    prev_interval: int,
) -> Card:
    """从持久化状态重建 Card；无历史时按已学间隔估算。"""
    card = Card()
    if state > 0:
        card.state = state
        card.stability = stability or max(prev_interval * 0.6, 1.0)
        card.difficulty = difficulty
    elif prev_interval > 1:
        card.state = 1  # Learning
        card.stability = max(prev_interval * 0.6, 1.0)
    return card


def compute_fsrs_review(
    rating: int,
    prev_interval: int,
    state: int = 0,
    stability: float = 0.0,
    difficulty: float = 0.0,
    today: date | None = None,
) -> FsrsState:
    """FSRS-6 调度。返回下一次复习状态（供 problems 表持久化）。"""
    today = today or date.today()
    card = _state_to_card(state, stability, difficulty, prev_interval)
    scheduler = Scheduler()
    review_dt = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    updated, _log = scheduler.review_card(
        card,
        Rating(rating),
        review_datetime=review_dt,
        review_duration=0,
    )
    due = updated.due
    scheduled = max(1, (due.date() - today).days)
    return FsrsState(
        state=int(updated.state),
        stability=round(updated.stability, 3),
        difficulty=round(updated.difficulty, 3),
        due=due.date().isoformat(),
        last_review=today.isoformat(),
        elapsed_days=max(0, (due.date() - today).days),
        scheduled_days=scheduled,
    )


def next_interval_days(
    rating: int,
    prev_interval: int,
    state: int = 0,
    stability: float = 0.0,
    difficulty: float = 0.0,
    today: date | None = None,
) -> int:
    """统一入口：FSRS 可用时用 FSRS，否则回退 SM-2 的间隔计算。"""
    if _FSRS_AVAILABLE:
        try:
            fs = compute_fsrs_review(rating, prev_interval, state, stability, difficulty, today)
            return max(1, fs.scheduled_days)
        except Exception as exc:
            LOG.warning("FSRS 调度异常，回退 SM-2: %s", exc)
    from review import compute_review
    return max(1, compute_review(rating, prev_interval, 2.5, 0).interval_days)
