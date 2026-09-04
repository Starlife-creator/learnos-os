"""ReviewsMixin — Handler 复习队列/完成/调度域。自 handler.py 原样迁移。"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

from config import LOG
from db import DB_LOCK, db, now, row, rows
from ai import get_cached_settings
from review import compute_review
from errors import ERROR_TYPE_LABELS, queue_weight
import fsrs_bridge
from fsrs_bridge import next_interval_days
from handler_base import (X_HEADER, X_VALUE, _IDEMPOTENCY, _IDEMPOTENCY_TTL,
                          _as_str_list, _interleave, _prune_idempotency)


class ReviewsMixin:
    def _handle_list_reviews(self) -> None:
        """复习队列。优先级排序（逾期久 > 错因权重 > 记忆脆弱 > 掌握度低 > 带漏点）
        + 交错 + 每日上限。?mode=plain 关闭交错（A7 遗留参数保留）。"""
        items = rows("""
            SELECT r.*, p.title, p.course, p.topic, p.content, p.my_attempt,
                   p.mastery, p.ease_factor, p.error_type, p.stability
            FROM reviews r JOIN problems p ON p.id = r.problem_id
            WHERE r.completed = 0 AND p.subject = ? ORDER BY r.due_date ASC
        """, (self.subject,))
        qs = parse_qs(urlparse(self.path).query)
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
        # 优先级（B3 M2+M3 双信号）：逾期久 > 知识性错因(M2 权重) > 记忆脆弱(M3 低稳定)
        # > 掌握度低 > 带费曼漏点。纯内存排序，零写库（F1 出队守恒条款）。
        today = date.today()
        items.sort(key=lambda r: (
            -(today - date.fromisoformat(r["due_date"])).days,
            -queue_weight(r["error_type"]),   # M2：空白/概念错(3) > 陷阱(2) > 计算/审题(1) > 执行类(0)
            float(r["stability"] or 0),       # M3：FSRS 稳定性低 = 记忆脆弱，优先重考
            float(r["mastery"] or 2.5),
            -int(r["feynman_gaps"]),
        ))
        # P0：交错复习默认开启（同知识点隔开）；?mode=plain 关闭
        if qs.get("mode", [""])[0] != "plain" and len(items) > 2:
            items = _interleave(items)
        # 每日上限（0 = 不限）：超出部分顺延到之后自然出现（Anki Easy-Days 思路）
        total = len(items)
        cap = int(get_cached_settings().get("daily_review_cap", "0") or 0)
        capped = 0 < cap < total
        if capped:
            items = items[:cap]
        self.json_response({"items": items, "total": total, "cap": cap, "capped": capped})

    def _handle_today_summary(self) -> None:
        """今日复盘摘要（A7）：统计今日复习结果 + 错因分布 + 明早温习建议。"""
        today = date.today().isoformat()
        done = rows("""
            SELECT r.result, p.error_type FROM reviews r JOIN problems p ON p.id = r.problem_id
            WHERE r.completed = 1 AND r.created_at >= ? AND p.subject = ?
        """, (today + "T00:00:00", self.subject))
        due_tomorrow = row("""
            SELECT COUNT(*) AS count FROM reviews WHERE completed = 0 AND due_date <= ?
            AND problem_id IN (SELECT id FROM problems WHERE subject = ?)
        """, ((date.today() + timedelta(days=1)).isoformat(), self.subject)) or {}
        done_count = len(done)
        hard = sum(1 for d in done if int(d["result"] or 0) <= 2)
        error_counts: dict[str, int] = {}
        for d in done:
            et = d["error_type"] or "待诊断"
            error_counts[et] = error_counts.get(et, 0) + 1
        top_error = sorted(error_counts.items(), key=lambda kv: -kv[1])[:1]
        top_error_label = ERROR_TYPE_LABELS.get(top_error[0][0], top_error[0][0]) if top_error else ""
        accuracy = round((1 - hard / done_count) * 100) if done_count else 0
        self.json_response({
            "date": today,
            "done": done_count,
            "accuracy": accuracy,
            "hard": hard,
            "top_error": top_error_label,
            "due_tomorrow": int(due_tomorrow.get("count", 0)),
            "error_counts": error_counts,
            "warmup": [3] if done_count else [],
        })

    def _handle_complete_review(self, review_id: int, data: dict[str, Any]) -> None:
        review = row("SELECT * FROM reviews WHERE id = ?", (review_id,))
        if not review:
            self.json_response({"error": "复习任务不存在"}, 404)
            return
        rating = max(1, min(4, int(data.get("rating", 2))))
        problem = row("SELECT ease_factor, repetition, state, stability, difficulty FROM problems WHERE id = ?", (review["problem_id"],))
        prev_ease = problem["ease_factor"] if problem else 2.5
        prev_rep = problem["repetition"] if problem else 0
        prev_state = int(problem["state"] or 0) if problem else 0
        prev_stability = float(problem["stability"] or 0) if problem else 0.0
        prev_difficulty = float(problem["difficulty"] or 0) if problem else 0.0

        result = compute_review(rating, review["interval_days"], prev_ease, prev_rep)
        fsrs_interval = next_interval_days(
            rating, review["interval_days"], prev_state, prev_stability, prev_difficulty,
        )
        if fsrs_interval != result.interval_days:
            result.interval_days = fsrs_interval
        # C6 逾期顺延（Forgiveness）：迟到复习按间隔减半重排；长逾期降级为新学
        overdue_days = max(0, (date.today() - date.fromisoformat(review["due_date"])).days)
        if overdue_days >= 21:
            result.repetition = 0
            result.ease_factor = min(result.ease_factor, 2.5)
            result.interval_days = 1
            result.mastery = max(1, result.mastery - 1)
        elif overdue_days >= 5:
            result.interval_days = max(1, result.interval_days // 2)
        next_due = (date.today() + timedelta(days=result.interval_days)).isoformat()
        # A1：FSRS 可用时同步持久化调度状态；否则状态列保留 0（SM-2 模式）
        # C6：长逾期降级为新学时不写 FSRS 成熟态，避免与新间隔矛盾
        if fsrs_bridge.fsrs_available() and overdue_days < 21:
            fs = fsrs_bridge.compute_fsrs_review(
                rating, review["interval_days"], prev_state, prev_stability, prev_difficulty,
            )
        else:
            fs = None
        with DB_LOCK, db() as conn:
            conn.execute("UPDATE reviews SET completed = 1, result = ? WHERE id = ?", (str(rating), review_id))
            conn.execute(
                "INSERT INTO reviews(problem_id, due_date, interval_days, variant_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (review["problem_id"], next_due, result.interval_days, int(review["variant_id"] or 0), now()),
            )
            if fs:
                conn.execute(
                    "UPDATE problems SET mastery = ?, ease_factor = ?, repetition = ?, state = ?, stability = ?, difficulty = ?, updated_at = ? WHERE id = ?",
                    (result.mastery, result.ease_factor, result.repetition, fs.state, fs.stability, fs.difficulty, now(), review["problem_id"]),
                )
            else:
                conn.execute(
                    "UPDATE problems SET mastery = ?, ease_factor = ?, repetition = ?, updated_at = ? WHERE id = ?",
                    (result.mastery, result.ease_factor, result.repetition, now(), review["problem_id"]),
                )
            self._log_variant_result(conn, review, rating)
            self._log_mastery(conn, self.subject)
        import graph
        graph.update_progress(self.subject, force=True, entry_point="review",
                              evidence=f"题目#{review['problem_id']} 评分{rating}")
        try:
            from gamification import record as gamify_record
            gamify_record(rating)  # D6 游戏化（零依赖，失败不影响主流程）
        except Exception as exc:
            LOG.debug("游戏化记录失败（可忽略）: %s", exc)
        self.json_response({"next_due": next_due, "interval_days": result.interval_days})

    def _handle_reschedule_review(self, review_id: int) -> None:
        """手动控制：把复习提前到今天。"""
        review = row("SELECT * FROM reviews WHERE id = ?", (review_id,))
        if not review:
            self.json_response({"error": "复习任务不存在"}, 404)
            return
        with DB_LOCK, db() as conn:
            conn.execute("UPDATE reviews SET due_date = ? WHERE id = ?", (date.today().isoformat(), review_id))
        self.json_response({"ok": True})
