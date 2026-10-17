"""复习会话领域层：队列排序、今日小结、完成复习的调度写回、手动顺延。

纯业务逻辑（SQL + 排序规则 + 事务），不含 HTTP 知识；`handler_reviews.py` 只做
薄适配。4xx 语义用 `ReviewError`（`DomainError` 子类）表达。

口径（ADR 0013）：队列、今日小结、顺延都是**题目复习页的专属视图**，只覆盖
`problems`/`reviews`；闪卡的复习行为不在这里，走 `cards.py` 与统一读模型视图。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from config import LOG
from db import DB_LOCK, db, now, row, rows
from ai import get_cached_settings
from errors import ERROR_TYPE_LABELS, queue_weight
from mastery import set_problem_mastery
from scheduler import schedule_review
from domain_errors import DomainError
import problems


class ReviewError(DomainError):
    """复习会话领域层的 4xx 语义（status 默认 400）。"""


def interleave(items: list[dict[str, Any]], key: str = "topic") -> list[dict[str, Any]]:
    """A7 交错练习：按 key 分桶后贪心轮转取卡，避免同知识点连续出现。

    每步选取剩余数量最多的桶且不等于上一个桶；若只剩一个桶则按原序补完。
    （原 `handler_base._interleave`，下沉到领域层以便脱离 HTTP 复用。）
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        k = str(item.get(key) or "未分类")
        buckets.setdefault(k, []).append(item)
    out: list[dict[str, Any]] = []
    last_key: str | None = None
    while buckets:
        candidates = [k for k in buckets if k != last_key]
        pool = candidates or list(buckets.keys())
        k = max(pool, key=lambda x: len(buckets[x]))
        out.append(buckets[k].pop(0))
        if not buckets[k]:
            del buckets[k]
        last_key = k
    return out


def queue(subject: str, *, plain: bool = False) -> dict[str, Any]:
    """待复习队列：优先级排序 + 交错 + 每日上限。

    优先级（B3 M2+M3 双信号）：逾期久 > 知识性错因（M2 权重）> 记忆脆弱（M3 低
    FSRS 稳定性）> 掌握度低 > 带费曼漏点。纯内存排序，零写库（F1 出队守恒条款）。
    `plain=True` 关闭交错（A7 遗留参数保留）。
    """
    items = rows("""
        SELECT r.*, p.title, p.course, p.topic, p.content, p.my_attempt,
               p.mastery, p.ease_factor, p.error_type, p.stability
        FROM reviews r JOIN problems p ON p.id = r.problem_id
        WHERE r.completed = 0 AND p.subject = ? ORDER BY r.due_date ASC
    """, (subject,))
    # A5：带未清漏点的题目标 Feynman 徽章（下次复习优先重考漏点）
    feynman_sessions = rows(
        "SELECT problem_id, self_review FROM oral_sessions "
        "WHERE mode = 'feynman' AND self_review != ''"
    )
    gap_counts: dict[int, int] = {}
    for fs in feynman_sessions:
        try:
            sr = json.loads(fs["self_review"])
            n = len(sr.get("gaps", []))
        except (json.JSONDecodeError, TypeError):
            n = 0
        if n:
            gap_counts[fs["problem_id"]] = max(gap_counts.get(fs["problem_id"], 0), n)
    for item in items:
        item["feynman_gaps"] = gap_counts.get(item["problem_id"], 0)
    today = date.today()
    items.sort(key=lambda r: (
        -(today - date.fromisoformat(r["due_date"])).days,
        -queue_weight(r["error_type"]),   # M2：空白/概念错(3) > 陷阱(2) > 计算/审题(1) > 执行类(0)
        float(r["stability"] or 0),       # M3：FSRS 稳定性低 = 记忆脆弱，优先重考
        float(r["mastery"] or 2.5),
        -int(r["feynman_gaps"]),
    ))
    if not plain and len(items) > 2:
        items = interleave(items)
    # 每日上限（0 = 不限）：超出部分顺延到之后自然出现（Anki Easy-Days 思路）
    total = len(items)
    cap = int(get_cached_settings().get("daily_review_cap", "0") or 0)
    capped = 0 < cap < total
    if capped:
        items = items[:cap]
    return {"items": items, "total": total, "cap": cap, "capped": capped}


def today_summary(subject: str) -> dict[str, Any]:
    """今日复盘摘要（A7）：统计今日复习结果 + 错因分布 + 明早温习建议。"""
    today = date.today().isoformat()
    done = rows("""
        SELECT r.result, p.error_type FROM reviews r JOIN problems p ON p.id = r.problem_id
        WHERE r.completed = 1 AND r.created_at >= ? AND p.subject = ?
    """, (today + "T00:00:00", subject))
    due_tomorrow = row("""
        SELECT COUNT(*) AS count FROM reviews WHERE completed = 0 AND due_date <= ?
        AND problem_id IN (SELECT id FROM problems WHERE subject = ?)
    """, ((date.today() + timedelta(days=1)).isoformat(), subject)) or {}
    done_count = len(done)
    hard = sum(1 for d in done if int(d["result"] or 0) <= 2)
    error_counts: dict[str, int] = {}
    for d in done:
        et = d["error_type"] or "待诊断"
        error_counts[et] = error_counts.get(et, 0) + 1
    top_error = sorted(error_counts.items(), key=lambda kv: -kv[1])[:1]
    top_error_label = ERROR_TYPE_LABELS.get(top_error[0][0], top_error[0][0]) if top_error else ""
    accuracy = round((1 - hard / done_count) * 100) if done_count else 0
    return {
        "date": today,
        "done": done_count,
        "accuracy": accuracy,
        "hard": hard,
        "top_error": top_error_label,
        "due_tomorrow": int(due_tomorrow.get("count", 0)),
        "error_counts": error_counts,
        "warmup": [3] if done_count else [],
    }


def complete(review_id: int, rating: Any, subject: str) -> dict[str, Any]:
    """完成一次复习：调度 + 事务内写完 reviews/problems/掌握度事件，返回下一轮排期。

    调度统一走 `scheduler.schedule_review`：FSRS 可用时 FSRS 全权决定，SM-2 降级支
    才做逾期顺延（ADR 0007）。本条复习任务的 `created_at` 即上一次复习完成时间
    （新建错题时为录入时间）。
    """
    review = row("SELECT * FROM reviews WHERE id = ?", (review_id,))
    if not review:
        raise ReviewError("复习任务不存在", 404)
    rating = max(1, min(4, int(rating)))
    problem = row("SELECT ease_factor, repetition, state, stability, difficulty FROM problems WHERE id = ?",
                  (review["problem_id"],)) or {}
    result = schedule_review(
        rating,
        prev_interval=review["interval_days"],
        prev_ease=problem.get("ease_factor") or 2.5,
        prev_repetition=problem.get("repetition") or 0,
        state=problem.get("state") or 0,
        stability=problem.get("stability") or 0.0,
        difficulty=problem.get("difficulty") or 0.0,
        due_date=review["due_date"],
        last_review=review["created_at"],
        subject=subject,
    )
    next_due = result.next_due
    with DB_LOCK, db() as conn:
        conn.execute("UPDATE reviews SET completed = 1, result = ? WHERE id = ?", (str(rating), review_id))
        conn.execute(
            "INSERT INTO reviews(problem_id, due_date, interval_days, variant_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (review["problem_id"], next_due, result.interval_days, int(review["variant_id"] or 0), now()),
        )
        conn.execute(
            "UPDATE problems SET ease_factor = ?, repetition = ?, state = ?, stability = ?, difficulty = ?, updated_at = ? WHERE id = ?",
            (result.ease_factor, result.repetition, result.state, result.stability,
             result.difficulty, now(), review["problem_id"]),
        )
        # ADR 0004：掌握度经单一写入口落事件
        set_problem_mastery(conn, review["problem_id"], result.mastery, "review",
                            f"题目#{review['problem_id']} 评分{rating}（{result.engine}）")
        problems.log_variant_result(conn, review, rating)
        problems.log_mastery(conn, subject)
    import graph
    graph.update_progress(subject, force=True, entry_point="review",
                          evidence=f"题目#{review['problem_id']} 评分{rating}")
    try:
        from gamification import record as gamify_record
        gamify_record(rating)  # D6 游戏化（零依赖，失败不影响主流程）
    except Exception as exc:
        LOG.debug("游戏化记录失败（可忽略）: %s", exc)
    return {"next_due": next_due, "interval_days": result.interval_days}


def reschedule(review_id: int) -> None:
    """手动控制：把复习提前到今天。"""
    review = row("SELECT * FROM reviews WHERE id = ?", (review_id,))
    if not review:
        raise ReviewError("复习任务不存在", 404)
    with DB_LOCK, db() as conn:
        conn.execute("UPDATE reviews SET due_date = ? WHERE id = ?", (date.today().isoformat(), review_id))
