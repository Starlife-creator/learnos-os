"""提示领域层：A6 诊断门与提示缓存读写。

提示的**生成**不在这里 —— AI 调用、降级文案、SSE 还是普通响应，都是 handler 的
决定（它要消费配额、要写流式响应头）。这里只回答三件事：要不要附诊断建议、
有没有存过的同档提示、把新提示存起来。
"""
from __future__ import annotations

from db import DB_LOCK, db, now, row


def diagnose_needed(problem_id: int) -> bool:
    """A6 诊断门：最近一次完成的复习是「忘记/模糊」（result <= 2）时返回 True。"""
    last = row("""
        SELECT result FROM reviews WHERE problem_id = ? AND completed = 1
        ORDER BY id DESC LIMIT 1
    """, (problem_id,))
    if not last:
        return False
    text = str(last["result"] or "").strip()
    return text.isdigit() and int(text) <= 2


def cached(problem_id: int, level: int) -> str | None:
    """同一题同一档已落库的提示内容；没有则 None（None 表示「未命中缓存」）。"""
    hit = row("SELECT content FROM hints WHERE problem_id = ? AND level = ?",
              (problem_id, level))
    return hit["content"] if hit else None


def save(problem_id: int, level: int, content: str) -> None:
    """落库一条提示缓存（同一题同档可有多条，读取取最新条件仅按 level 命中）。"""
    with DB_LOCK, db() as conn:
        conn.execute(
            "INSERT INTO hints(problem_id, level, content, created_at) VALUES (?, ?, ?, ?)",
            (problem_id, level, content, now()),
        )
