"""D6 游戏化防刷：本地 XP / 连续天数 / 徽章（零依赖，无排行榜，无内购）。

规则：
- 复习打卡：当日完成 ≥1 张卡才记日打卡（复习接口内累计）。
- XP 按 FSRS rating 结算：忘记1 → 1，模糊2 → 3，基本正确3 → 8，掌握4 → 15。
- 闪电复习同样走复习接口（统一口径，不做双轨）。
- 徽章本地计算：首战 / 连胜 7 / 连胜 30 / 累计 100 题 / 单日 20 题 / 累计 500 XP。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from config import LOG
from db import DB_LOCK, db

_RATING_XP = {1: 1, 2: 3, 3: 8, 4: 15}
_BADGES: list[tuple[str, str]] = [
    ("first_blood", "首战告捷：完成第一次复习"),
    ("streak_7", "七日连胜：连续 7 天打卡"),
    ("streak_30", "月度全勤：连续 30 天打卡"),
    ("reviews_100", "百题斩：累计复习 100 题"),
    ("daily_20", "冲刺日：单日复习 20 题"),
    ("xp_500", "老司机：累计 500 XP"),
]


def record(rating: int) -> None:
    """复习完成后累计当日 XP 与题数（幂等由调用方保证一次）。"""
    xp = _RATING_XP.get(max(1, min(4, int(rating))), 1)
    today = date.today().isoformat()
    try:
        with DB_LOCK, db() as conn:
            conn.execute(
                "INSERT INTO gamification(date, reviews, xp) VALUES (?, 1, ?) "
                "ON CONFLICT(date) DO UPDATE SET reviews = reviews + 1, xp = xp + excluded.xp",
                (today, xp),
            )
    except Exception as exc:
        LOG.debug("游戏化写入失败（可忽略）: %s", exc)


def state() -> dict[str, Any]:
    """当前游戏化状态：总 XP/题数、今日数据、连续天数、已解锁徽章。"""
    today = date.today().isoformat()
    try:
        rows = _rows("SELECT date, reviews, xp FROM gamification")
    except Exception as exc:
        LOG.debug("游戏化读取失败（可忽略）: %s", exc)
        return {}
    daily = {r["date"]: r for r in rows}
    total_xp = sum(r["xp"] for r in rows)
    total_reviews = sum(r["reviews"] for r in rows)
    streak = 0
    cursor = date.today()
    while cursor.isoformat() in daily:
        streak += 1
        cursor -= timedelta(days=1)
    today_entry = daily.get(today)
    badges = []
    if total_reviews > 0:
        badges.append("first_blood")
    if streak >= 7:
        badges.append("streak_7")
    if streak >= 30:
        badges.append("streak_30")
    if total_reviews >= 100:
        badges.append("reviews_100")
    if today_entry and today_entry["reviews"] >= 20:
        badges.append("daily_20")
    if total_xp >= 500:
        badges.append("xp_500")
    return {
        "total_xp": total_xp,
        "total_reviews": total_reviews,
        "today_xp": int(today_entry["xp"]) if today_entry else 0,
        "today_reviews": int(today_entry["reviews"]) if today_entry else 0,
        "streak": streak,
        "badges": [{"id": bid, "label": label, "unlocked": bid in badges}
                   for bid, label in _BADGES],
    }


def _rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    from db import rows
    return rows(query, params)


__all__ = ["record", "state"]
