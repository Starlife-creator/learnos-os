"""统一复习调度入口（题目复习 / 闪卡复习共用）。

决策（见 docs/decisions/0007-scheduler-split.md）：
- FSRS 可用：间隔与记忆态（state/stability/difficulty）完全由 FSRS 给出。
  FSRS 按「距上次复习的真实天数」建模遗忘，逾期复习已体现在新的稳定性里，
  因此不再叠加逾期顺延规则（否则同一段逾期被惩罚两次）。
- FSRS 不可用或异常：退回 SM-2，逾期顺延（Forgiveness）只在这一支生效：
  逾期 >=21 天降级为新学（interval=1、repetition=0、掌握度减一），
  逾期 >=5 天间隔减半。
- SM-2 的 ease/repetition/mastery 两支都会计算：mastery（1-5 级）是展示与图谱聚合
  用的等级，不参与 FSRS 调度。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import fsrs_bridge
from config import LOG
from review import compute_review

LONG_OVERDUE_DAYS = 21   # SM-2 支：降级为新学
OVERDUE_HALVE_DAYS = 5   # SM-2 支：间隔减半


@dataclass
class ScheduleResult:
    engine: str            # "fsrs" | "sm2"
    interval_days: int
    next_due: str          # ISO 日期
    ease_factor: float
    repetition: int
    mastery: int           # 1-5 级
    state: int
    stability: float
    difficulty: float
    overdue_days: int


def _parse_day(value: object) -> date | None:
    """接受 'YYYY-MM-DD' 或带时间的 ISO 串；无效返回 None。"""
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def overdue_days(due_date: object, today: date | None = None) -> int:
    due = _parse_day(due_date)
    if due is None:
        return 0
    return max(0, ((today or date.today()) - due).days)


def schedule_review(
    rating: int,
    *,
    prev_interval: int,
    prev_ease: float = 2.5,
    prev_repetition: int = 0,
    state: int = 0,
    stability: float = 0.0,
    difficulty: float = 0.0,
    due_date: object = "",
    last_review: object = "",
    today: date | None = None,
    subject: str = "",
) -> ScheduleResult:
    """计算一次复习后的完整调度结果（纯函数，不读写数据库）。"""
    today = today or date.today()
    rating = max(1, min(4, int(rating or 2)))
    prev_interval = max(1, int(prev_interval or 1))
    sm2 = compute_review(rating, prev_interval, float(prev_ease or 2.5), int(prev_repetition or 0))
    late = overdue_days(due_date, today)
    last = _parse_day(last_review)

    if fsrs_bridge.fsrs_available():
        try:
            fs = fsrs_bridge.compute_fsrs_review(
                rating, prev_interval, int(state or 0), float(stability or 0.0),
                float(difficulty or 0.0), today=today, subject=subject,
                last_review=last.isoformat() if last else "",
            )
            interval = max(1, fs.scheduled_days)
            return ScheduleResult(
                engine="fsrs", interval_days=interval,
                next_due=(today + timedelta(days=interval)).isoformat(),
                ease_factor=sm2.ease_factor, repetition=sm2.repetition, mastery=sm2.mastery,
                state=fs.state, stability=fs.stability, difficulty=fs.difficulty,
                overdue_days=late,
            )
        except Exception as exc:  # 调度异常不阻断复习，退回 SM-2
            LOG.warning("FSRS 调度异常，回退 SM-2: %s", exc)

    interval, ease, rep, mastery = sm2.interval_days, sm2.ease_factor, sm2.repetition, sm2.mastery
    # SM-2 模式不推进 FSRS 记忆态，原样保留（FSRS 恢复后从此处继续）
    st, stab, diff = int(state or 0), float(stability or 0.0), float(difficulty or 0.0)
    if late >= LONG_OVERDUE_DAYS:
        interval, rep, ease = 1, 0, min(ease, 2.5)
        mastery = max(1, mastery - 1)
        st, stab, diff = 0, 0.0, 0.0  # 降级为新学：记忆态归零，避免与新间隔矛盾
    elif late >= OVERDUE_HALVE_DAYS:
        interval = max(1, interval // 2)
    return ScheduleResult(
        engine="sm2", interval_days=interval,
        next_due=(today + timedelta(days=interval)).isoformat(),
        ease_factor=ease, repetition=rep, mastery=mastery,
        state=st, stability=stab, difficulty=diff, overdue_days=late,
    )
