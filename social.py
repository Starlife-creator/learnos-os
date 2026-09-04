"""学习小组本地优先打卡与无答案进度分享（§34.2/§42.3）。

设计原则：
- 打卡只记录「日期 / 学科 / 时长 / 自由备注」，绝不落任何题目或答案。
- 分享导出只包含聚合进度（连续天数、掌握度、薄弱主题、待复习数），
  不暴露 content / my_attempt / hint 等任何可还原答案的字段，满足「本地优先、
  可安全分享给学习小组」的隐私约束。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from db import DB_LOCK, db, now, row, rows


def add_checkin(subject: str, minutes: int, note: str = "", check_date: str | None = None) -> int:
    """新增一条打卡；check_date 缺省为今天。minutes 钳制到 [0, 1440]。"""
    day = (check_date or date.today().isoformat()).strip()
    if not day:
        day = date.today().isoformat()
    minutes = max(0, min(1440, int(minutes or 0)))
    note = (note or "").strip()[:280]
    subj = (subject or "").strip()[:40]
    with DB_LOCK, db() as conn:
        cur = conn.execute(
            "INSERT INTO study_checkins(check_date, subject, minutes, note, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (day, subj, minutes, note, now()),
        )
        return int(cur.lastrowid)


def list_checkins(subject: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
    """最近打卡列表（默认全部学科）。"""
    limit = max(1, min(200, int(limit or 30)))
    if subject:
        data = rows(
            "SELECT id, check_date, subject, minutes, note, created_at FROM study_checkins "
            "WHERE subject = ? ORDER BY check_date DESC, id DESC LIMIT ?",
            (subject, limit),
        )
    else:
        data = rows(
            "SELECT id, check_date, subject, minutes, note, created_at FROM study_checkins "
            "ORDER BY check_date DESC, id DESC LIMIT ?",
            (limit,),
        )
    return [dict(r) for r in data]


def _dates_set(subject: str | None) -> set[str]:
    """该学科（或全部）所有打卡日期集合。"""
    if subject:
        data = rows("SELECT DISTINCT check_date FROM study_checkins WHERE subject = ?", (subject,))
    else:
        data = rows("SELECT DISTINCT check_date FROM study_checkins")
    return {r["check_date"] for r in data}


def streak(subject: str | None = None) -> int:
    """连续打卡天数：从今天往回数，若今天没打卡则从昨天往回（允许今天尚未打卡）。"""
    dates = _dates_set(subject)
    if not dates:
        return 0
    today = date.today()
    # 若今天已打卡，从今天起数；否则从昨天起数（今天尚未打卡不中断连续）。
    start = today if today.isoformat() in dates else today - timedelta(days=1)
    count = 0
    cur = start
    while cur.isoformat() in dates:
        count += 1
        cur -= timedelta(days=1)
    return count


def total_minutes(subject: str | None = None) -> int:
    if subject:
        r = row("SELECT COALESCE(SUM(minutes), 0) AS s FROM study_checkins WHERE subject = ?", (subject,))
    else:
        r = row("SELECT COALESCE(SUM(minutes), 0) AS s FROM study_checkins")
    return int(r["s"]) if r else 0


def export_social(subject: str | None = None) -> dict[str, Any]:
    """无答案进度分享包（供本地导出给学习小组）。

    仅含聚合指标与主题级薄弱点，**不包含任何题目内容或答案**。
    """
    # 无 subject 时用恒真条件兜底：weak 查询拼接 `AND topic <> ''`，空 where 会产生非法 SQL
    where = "WHERE subject = ?" if subject else "WHERE 1=1"
    params = (subject,) if subject else ()
    stats = row(
        f"SELECT COUNT(*) AS total, COALESCE(AVG(mastery), 0) AS avg_mastery, "
        f"COALESCE(SUM(CASE WHEN mastery >= 4 THEN 1 ELSE 0 END), 0) AS mastered "
        f"FROM problems {where}",
        params,
    ) or {"total": 0, "avg_mastery": 0, "mastered": 0}
    weak = rows(
        f"SELECT topic, COUNT(*) AS count, ROUND(AVG(mastery), 1) AS mastery "
        f"FROM problems {where} AND topic <> '' GROUP BY topic ORDER BY mastery ASC, count DESC LIMIT 5",
        params,
    )
    due = row(
        f"SELECT COUNT(*) AS c FROM reviews WHERE completed = 0 "
        f"AND due_date <= ? AND problem_id IN (SELECT id FROM problems {where})",
        (date.today().isoformat(), *params),
    ) or {"c": 0}
    return {
        "schema": "learnos-social-share/1",
        "subject": subject or "*",
        "exported_at": now(),
        "streak_days": streak(subject),
        "total_minutes": total_minutes(subject),
        "total_problems": int(stats["total"]),
        "mastered": int(stats["mastered"]),
        "avg_mastery": round(float(stats["avg_mastery"]), 1),
        "due_today": int(due["c"]),
        "weak_topics": [
            {"topic": w["topic"], "avg_mastery": float(w["mastery"]), "count": int(w["count"])}
            for w in weak
        ],
        "note": "本分享不包含任何题目内容与答案（本地优先隐私约束）。",
    }
