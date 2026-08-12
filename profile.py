"""C5 学习者档案：画像聚合（实时计算）+ 偏好/目标持久化。

隐私（R）：档案仅本地存储与展示；不随遥测外发、导出外部前须用户确认。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from db import DB_LOCK, db, row, rows, now

_DEFAULTS: dict[str, str] = {
    "explain_depth": "2",      # 讲解深度偏好 1-3
    "example_count": "1",      # 例题数偏好 0-3
    "daily_review_target": "", # 每日复习目标（0/空 = 不限）
    "exam_date": "",
    "exam_target_score": "",
    "note": "",
}


def _get_all() -> dict[str, str]:
    data = dict(_DEFAULTS)
    for r in rows("SELECT key, value FROM learner_profile"):
        data[r["key"]] = r["value"]
    return data


def aggregate() -> dict[str, Any]:
    """实时画像：知识点掌握度 / 错因分布 / 学习节奏 / 目标。"""
    today = date.today()
    week_ago = (today - timedelta(days=6)).isoformat()
    month_ago = (today - timedelta(days=29)).isoformat()

    topics = rows("""
        SELECT topic, COUNT(*) AS count, ROUND(AVG(mastery), 1) AS avg_mastery
        FROM problems WHERE topic <> '' GROUP BY topic
        ORDER BY avg_mastery ASC, count DESC LIMIT 8
    """)
    errors = rows("""
        SELECT error_type, COUNT(*) AS count FROM problems
        WHERE error_type <> '' AND error_type <> '待诊断' GROUP BY error_type ORDER BY count DESC LIMIT 8
    """)
    pace = {
        "week_reviews": int((row("SELECT COUNT(*) AS c FROM reviews WHERE completed = 1 AND created_at >= ?", (week_ago,)) or {}).get("c", 0)),
        "month_reviews": int((row("SELECT COUNT(*) AS c FROM reviews WHERE completed = 1 AND created_at >= ?", (month_ago,)) or {}).get("c", 0)),
        "week_new_problems": int((row("SELECT COUNT(*) AS c FROM problems WHERE created_at >= ?", (week_ago,)) or {}).get("c", 0)),
    }
    # 最近活跃时段（复习完成的钟点分布），取最高频
    hour_row = rows("""
        SELECT CAST(substr(created_at, 12, 2) AS INTEGER) AS hour, COUNT(*) AS c
        FROM reviews WHERE completed = 1 GROUP BY hour ORDER BY c DESC LIMIT 1
    """)
    pace["peak_hour"] = int(hour_row[0]["hour"]) if hour_row else 0
    prefs = _get_all()
    return {
        "topics": topics,
        "errors": errors,
        "pace": pace,
        "preferences": {
            "explain_depth": prefs.get("explain_depth", "2"),
            "example_count": prefs.get("example_count", "1"),
        },
        "goal": {
            "exam_date": prefs.get("exam_date", ""),
            "exam_target_score": prefs.get("exam_target_score", ""),
            "daily_review_target": prefs.get("daily_review_target", ""),
            "note": prefs.get("note", ""),
        },
    }


def snapshot(max_topics: int = 5, max_errors: int = 5) -> str:
    """AI 请求附带的档案摘要（约 300-500 token，隐私仅限本地）。"""
    a = aggregate()
    topic_line = "；".join(
        f"{t['topic']} 掌握{t['avg_mastery']}/5" for t in a["topics"][:max_topics]
    ) or "暂无知识点数据"
    error_line = "；".join(f"{e['error_type']}×{e['count']}" for e in a["errors"][:max_errors]) or "无"
    goal = a["goal"]
    goal_line = "未设定"
    if goal["exam_date"]:
        days = (date.fromisoformat(goal["exam_date"]) - date.today()).days
        goal_line = f"考试 {goal['exam_date']}（剩 {days} 天）"
        if goal["exam_target_score"]:
            goal_line += f"，目标 {goal['exam_target_score']}"
    pace = a["pace"]
    return (
        f"【学习者档案】知识点掌握度：{topic_line}。"
        f"错因分布：{error_line}。"
        f"近7天复习{pace['week_reviews']}次、新增{pace['week_new_problems']}题，"
        f"常活跃在 {pace['peak_hour']} 时前后。"
        f"偏好讲解深度{'深' if int(a['preferences']['explain_depth']) >= 3 else '中' if int(a['preferences']['explain_depth']) == 2 else '浅'}。"
        f"目标：{goal_line}。"
    )


def update(values: dict[str, Any]) -> None:
    """保存偏好/目标（仅白名单键）。"""
    allowed = set(_DEFAULTS)
    pairs = [(k, str(v).strip()) for k, v in values.items() if k in allowed and str(v).strip() != ""]
    with DB_LOCK, db() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO learner_profile(key, value) VALUES (?, ?)", pairs,
        )
        # 显式提交空串表示清除
        for k, v in values.items():
            if k in allowed and str(v).strip() == "" and _get_all().get(k, "") != "":
                conn.execute("DELETE FROM learner_profile WHERE key = ?", (k,))