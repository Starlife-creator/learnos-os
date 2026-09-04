"""ReportsMixin — Handler 仪表盘/周期报告/趋势/分析域。自 handler.py 原样迁移。"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from config import LOG
from db import DB_LOCK, db, row, rows
import fsrs_bridge
from handler_base import (X_HEADER, X_VALUE, _IDEMPOTENCY, _IDEMPOTENCY_TTL,
                          _as_str_list, _interleave, _prune_idempotency)


class ReportsMixin:
    def _handle_dashboard(self) -> None:
        today = date.today().isoformat()
        subj = self.subject
        stats = row("""
            SELECT COUNT(*) AS total,
                   COALESCE(AVG(mastery), 0) AS avg_mastery,
                   SUM(CASE WHEN mastery >= 4 THEN 1 ELSE 0 END) AS mastered
            FROM problems WHERE subject = ?
        """, (subj,)) or {}
        due = row("SELECT COUNT(*) AS count FROM reviews WHERE completed = 0 AND due_date <= ? AND problem_id IN (SELECT id FROM problems WHERE subject = ?)", (today, subj)) or {}
        topics = rows("""
            SELECT topic, COUNT(*) AS count, ROUND(AVG(mastery), 1) AS mastery
            FROM problems WHERE subject = ? AND topic <> '' GROUP BY topic ORDER BY mastery ASC, count DESC LIMIT 8
        """, (subj,))
        # M2（B3）：薄弱错因知识点最高优先进口试——选题逻辑已抽到
        # graph.weak_oral_topic（U1 next_step 复用同一函数，保证不因入口分叉）。
        import graph
        oral_topic = graph.weak_oral_topic(subj, topics)
        recent = rows("SELECT id, title, course, topic, error_type, mastery, created_at, starred FROM problems WHERE subject = ? ORDER BY id DESC LIMIT 5", (subj,))
        recent_activity = rows("""
            SELECT r.id, r.result, r.created_at, p.id AS problem_id, p.title, p.course, p.topic
            FROM reviews r JOIN problems p ON p.id = r.problem_id
            WHERE r.completed = 1 AND p.subject = ? ORDER BY r.id DESC LIMIT 5
        """, (subj,))
        course_stats = rows("""
            SELECT course, COUNT(*) AS count, ROUND(AVG(mastery), 1) AS avg_mastery,
                   SUM(CASE WHEN mastery >= 4 THEN 1 ELSE 0 END) AS mastered,
                   (SELECT COUNT(*) FROM reviews WHERE completed=0 AND due_date<=? AND problem_id IN (SELECT id FROM problems p2 WHERE p2.course = p.course)) AS due
            FROM problems p WHERE p.subject = ? AND course <> '' GROUP BY course ORDER BY avg_mastery ASC LIMIT 6
        """, (subj, today))
        # C6 合并仪表盘数据：趋势 / 分析 / 错因分布（单请求）
        trend = self._trend_data(subj)
        analytics = self._analytics_data(subj)
        # U1：统一 next_step（与 /api/learn/next-step 同函数，队列消费同一结果）
        next_step = graph.next_step(subj)
        self.json_response({
            "stats": stats, "due": due["count"] if due else 0, "topics": topics,
            "oral_topic": oral_topic,
            "next_step": next_step,
            "recent": recent, "recent_activity": recent_activity, "course_stats": course_stats,
            "points": trend["points"], "summary": trend["summary"],
            "due_7d": analytics["due_7d"], "deck_health": analytics["deck_health"],
            "daily_reviews": analytics["daily_reviews"],
            "error_distribution": self._error_distribution(subj),
            "error_trend": self._error_trend(subj),
            "pressure": self._pressure_index(subj),
            "forget_predict": self._forget_predict(subj),
            "tasks": self._today_tasks(subj),
            "stubborn": self._stubborn_problems(subj),
            "gamification": self._game_state(),
            "telemetry": self._telemetry_summary(),
            "weekly": self._weekly_report(subj),
        })

    @staticmethod
    def _game_state() -> dict[str, Any]:
        try:
            from gamification import state as game_state
            return game_state()
        except Exception as exc:
            LOG.debug("游戏化状态失败（可忽略）: %s", exc)
            return {}

    @staticmethod
    def _telemetry_summary() -> dict[str, Any]:
        """仪表盘遥测卡片：委托 telemetry.summary()，失败降级为空字典（§1.3 韧性）。"""
        try:
            import telemetry
            return telemetry.summary()
        except Exception as exc:
            LOG.debug("遥测汇总失败（可忽略）: %s", exc)
            return {}

    @staticmethod
    def _activity_days(subject: str) -> set[str]:
        """§42.2：学习活跃日集合（完成复习或新建错题的日期），用于连续天数。"""
        days: set[str] = set()
        for r in rows(
            "SELECT DATE(created_at) AS d FROM reviews "
            "WHERE completed = 1 AND problem_id IN (SELECT id FROM problems WHERE subject = ?)", (subject,)
        ):
            if r["d"]:
                days.add(r["d"])
        for r in rows("SELECT DATE(created_at) AS d FROM problems WHERE subject = ?", (subject,)):
            if r["d"]:
                days.add(r["d"])
        return days

    @staticmethod
    def _streak(subject: str) -> int:
        """§42.1：连续学习天数（截至今天，含今天若有活动）。"""
        days = ReportsMixin._activity_days(subject)
        if not days:
            return 0
        streak = 0
        cur = date.today()
        # 今天无活动则从昨天往前数
        if cur.isoformat() not in days:
            cur = cur - timedelta(days=1)
        while cur.isoformat() in days:
            streak += 1
            cur = cur - timedelta(days=1)
        return streak

    def _handle_progress(self) -> None:
        """§42.1/§42.2 可见进步仪表盘 + 微学习节奏（≤10 分钟单元）。

        对抗 85% 三周流失：让「努力→可见进步→奖励→回返」的循环每会话可呈现。
        """
        subj = self.subject
        today = date.today().isoformat()
        stats = row("""
            SELECT COUNT(*) AS total,
                   COALESCE(AVG(mastery), 0) AS avg_mastery,
                   SUM(CASE WHEN mastery >= 4 THEN 1 ELSE 0 END) AS mastered
            FROM problems WHERE subject = ?
        """, (subj,)) or {}
        total = int(stats.get("total") or 0)
        avg_mastery = float(stats.get("avg_mastery") or 0)
        mastered = int(stats.get("mastered") or 0)
        due = row(
            "SELECT COUNT(*) AS count FROM reviews "
            "WHERE completed = 0 AND due_date <= ? AND problem_id IN (SELECT id FROM problems WHERE subject = ?)",
            (today, subj),
        ) or {}
        due_today = int(due.get("count") or 0)
        weak = rows("""
            SELECT topic, ROUND(AVG(mastery), 1) AS mastery, COUNT(*) AS count
            FROM problems WHERE subject = ? AND topic <> ''
            GROUP BY topic ORDER BY mastery ASC, count DESC LIMIT 3
        """, (subj,))
        # §42.2 微学习单元（≤10 分钟）：到期卡优先，薄弱主题补 1 题费曼/重做
        review_target = min(due_today, 10)
        micro_steps = []
        if review_target > 0:
            micro_steps.append(f"复习 {review_target} 张到期卡")
        if weak:
            micro_steps.append(f"重做 1 道薄弱主题「{weak[0]['topic']}」错题")
        if micro_steps:
            micro_steps.append("用费曼口述 1 个概念（≤3 分钟）")
        else:
            micro_steps.append("录入 1 道新错题或读 1 节材料（≤10 分钟）")
        plan_minutes = max(5, len(micro_steps) * 3)
        self.json_response({
            "subject": subj,
            "mastery_pct": round(avg_mastery / 5 * 100, 1),
            "mastered": mastered,
            "total": total,
            "due_today": due_today,
            "streak_days": self._streak(subj),
            "weak_topics": weak,
            "micro_unit": {
                "steps": micro_steps,
                "est_minutes": min(plan_minutes, 10),
                "fits_under_10min": plan_minutes <= 10,
            },
        })

    @staticmethod
    def _weekly_report(subject: str = "physics") -> dict[str, Any]:
        """D5 学习日志周报：本周 vs 上周变化 + 模板建议（零依赖，AI 可选）。"""
        from datetime import date, timedelta
        today = date.today()
        week_start = today - timedelta(days=today.weekday())
        prev_start = week_start - timedelta(days=7)
        try:
            with DB_LOCK, db() as conn:
                counts = conn.execute("""
                    SELECT
                      SUM(CASE WHEN date(created_at) >= ? AND date(created_at) <= ? THEN 1 ELSE 0 END) AS new_problems,
                      SUM(CASE WHEN date(created_at) >= ? AND date(created_at) < ? THEN 1 ELSE 0 END) AS prev_problems,
                      COUNT(*) AS total
                    FROM problems WHERE subject = ?
                """, (week_start.isoformat(), today.isoformat(),
                      prev_start.isoformat(), week_start.isoformat(), subject)).fetchone()
                reviews = conn.execute("""
                    SELECT
                      SUM(CASE WHEN date(created_at) >= ? THEN 1 ELSE 0 END) AS week_reviews,
                      SUM(CASE WHEN date(created_at) >= ? AND date(created_at) < ? THEN 1 ELSE 0 END) AS prev_reviews,
                      SUM(CASE WHEN date(created_at) >= ? AND CAST(result AS INTEGER) >= 3 THEN 1 ELSE 0 END) AS week_good
                    FROM reviews WHERE completed = 1 AND problem_id IN (SELECT id FROM problems WHERE subject = ?)
                """, (week_start.isoformat(), prev_start.isoformat(),
                      week_start.isoformat(), week_start.isoformat(), subject)).fetchone()
        except Exception as exc:
            LOG.debug("周报统计失败（可忽略）: %s", exc)
            return {}
        new_problems = int(counts["new_problems"] or 0)
        prev_problems = int(counts["prev_problems"] or 0)
        week_reviews = int(reviews["week_reviews"] or 0)
        prev_reviews = int(reviews["prev_reviews"] or 0)
        week_good = int(reviews["week_good"] or 0)
        good_rate = round(week_good / week_reviews, 3) if week_reviews else 0.0
        tip_key = "report.tipWeekNone" if week_reviews == 0 else \
                  "report.tipWeekGood" if good_rate >= 0.7 else \
                  "report.tipWeekLow"
        return {
            "week_start": week_start.isoformat(),
            "new_problems": new_problems,
            "prev_problems": prev_problems,
            "week_reviews": week_reviews,
            "prev_reviews": prev_reviews,
            "good_rate": good_rate,
            "tip_key": tip_key,
            "review_delta": week_reviews - prev_reviews,
        }

    def _handle_weekly_report(self) -> None:
        """GET /api/report/weekly：周报详情（供前端详情弹窗）。"""
        self.json_response(self._weekly_report(self.subject))

    @staticmethod
    def _monthly_report(subject: str = "physics") -> dict[str, Any]:
        """近 30 天周期报告：复习/新增/保持率/错因分布/活跃天数 + 模板建议（零依赖）。"""
        from datetime import date, timedelta
        today = date.today()
        month_start = today - timedelta(days=29)
        prev_start = month_start - timedelta(days=30)
        prev_end = month_start - timedelta(days=1)
        try:
            with DB_LOCK, db() as conn:
                probs = conn.execute("""
                    SELECT
                      SUM(CASE WHEN date(created_at) >= ? THEN 1 ELSE 0 END) AS month_new,
                      SUM(CASE WHEN date(created_at) >= ? AND date(created_at) <= ? THEN 1 ELSE 0 END) AS prev_new
                    FROM problems WHERE subject = ?
                """, (month_start.isoformat(), prev_start.isoformat(), prev_end.isoformat(), subject)).fetchone()
                revs = conn.execute("""
                    SELECT
                      SUM(CASE WHEN date(created_at) >= ? THEN 1 ELSE 0 END) AS month_revs,
                      SUM(CASE WHEN date(created_at) >= ? AND date(created_at) <= ? THEN 1 ELSE 0 END) AS prev_revs,
                      SUM(CASE WHEN date(created_at) >= ? AND CAST(result AS INTEGER) >= 3 THEN 1 ELSE 0 END) AS month_good,
                      COUNT(DISTINCT CASE WHEN date(created_at) >= ? THEN date(created_at) END) AS active_days
                    FROM reviews WHERE completed = 1 AND problem_id IN (SELECT id FROM problems WHERE subject = ?)
                """, (month_start.isoformat(), prev_start.isoformat(), prev_end.isoformat(),
                      month_start.isoformat(), month_start.isoformat(), subject)).fetchone()
                total = conn.execute(
                    "SELECT COUNT(*) AS c, "
                    "SUM(CASE WHEN mastery >= 4 THEN 1 ELSE 0 END) AS mastered FROM problems WHERE subject = ?",
                    (subject,),
                ).fetchone()
                errs = conn.execute("""
                    SELECT p.error_type AS et, COUNT(*) AS c
                    FROM reviews r JOIN problems p ON p.id = r.problem_id
                    WHERE r.completed = 1 AND p.subject = ? AND date(r.created_at) >= ?
                    GROUP BY p.error_type ORDER BY c DESC LIMIT 5
                """, (subject, month_start.isoformat())).fetchall()
                dailies = conn.execute("""
                    SELECT date(created_at) AS d, COUNT(*) AS c
                    FROM reviews WHERE completed = 1 AND date(created_at) >= ?
                    AND problem_id IN (SELECT id FROM problems WHERE subject = ?)
                    GROUP BY date(created_at) ORDER BY d
                """, (month_start.isoformat(), subject)).fetchall()
        except Exception as exc:
            LOG.debug("月报统计失败（可忽略）: %s", exc)
            return {}
        month_new = int(probs["month_new"] or 0)
        prev_new = int(probs["prev_new"] or 0)
        month_revs = int(revs["month_revs"] or 0)
        prev_revs = int(revs["prev_revs"] or 0)
        month_good = int(revs["month_good"] or 0)
        active = int(revs["active_days"] or 0)
        good_rate = round(month_good / month_revs, 3) if month_revs else 0.0
        mastery = int(total["mastered"] or 0)
        tip_key = "report.tipMonthNone" if month_revs == 0 else \
                  "report.tipMonthGood" if good_rate >= 0.7 else \
                  "report.tipMonthLow"
        return {
            "start": month_start.isoformat(),
            "end": today.isoformat(),
            "month_new": month_new,
            "prev_new": prev_new,
            "month_revs": month_revs,
            "prev_revs": prev_revs,
            "good_rate": good_rate,
            "active_days": active,
            "mastered": mastery,
            "total_problems": int(total["c"] or 0),
            "top_errors": [{"label": e["et"], "count": int(e["c"])} for e in errs],
            "daily": [{"date": d["d"], "count": int(d["c"])} for d in dailies],
            "tip_key": tip_key,
        }

    def _handle_monthly_report(self) -> None:
        """GET /api/report/monthly：近 30 天周期报告详情。"""
        self.json_response(self._monthly_report(self.subject))

    @staticmethod
    def _stubborn_problems(subject: str = "physics") -> list[dict[str, Any]]:
        """P0 顽固错题：同题评分(<=2)达 2 次的题，按失手次数排序（可指定学科）。"""
        items = rows("""
            SELECT p.id, p.title, p.topic, p.mastery, p.repetition,
                   (SELECT COUNT(*) FROM reviews r
                    WHERE r.problem_id = p.id AND r.completed = 1
                      AND CAST(r.result AS INTEGER) <= 2) AS miss_count,
                   (SELECT COUNT(*) FROM reviews r
                    WHERE r.problem_id = p.id AND r.completed = 1) AS total_reviews
            FROM problems p
            WHERE p.subject = ? AND (SELECT COUNT(*) FROM reviews r
                   WHERE r.problem_id = p.id AND r.completed = 1
                     AND CAST(r.result AS INTEGER) <= 2) >= 2
            ORDER BY miss_count DESC, p.mastery ASC
            LIMIT 6
        """, (subject,))
        out = []
        for p in items:
            out.append({
                "id": p["id"], "title": p["title"], "topic": p["topic"],
                "mastery": p["mastery"], "repetition": p["repetition"],
                "miss_count": p["miss_count"] or 0,
                "total_reviews": p["total_reviews"] or 0,
            })
        return out

    @staticmethod
    def _forget_predict(subject: str = "physics") -> dict[str, Any]:
        """P0 遗忘预测：对近期到期复习用 FSRS 预测 R（可指定学科）。"""
        import fsrs_bridge
        soon = rows("""
            SELECT r.id, r.problem_id, r.due_date, r.interval_days,
                   p.state, p.stability, p.difficulty, p.title,
                   (SELECT MAX(created_at) FROM reviews
                    WHERE problem_id = p.id AND completed = 1) AS last_review
            FROM reviews r JOIN problems p ON p.id = r.problem_id
            WHERE r.completed = 0 AND r.due_date <= ? AND p.subject = ?
            ORDER BY r.due_date ASC
        """, ((date.today() + timedelta(days=1)).isoformat(), subject))
        if not soon:
            return {"count": 0, "high_risk": 0, "medium_risk": 0, "avg_r": None, "top": []}
        values = []
        for s in soon:
            r = fsrs_bridge.retrievability(
                prev_interval=int(s["interval_days"] or 1),
                state=int(s["state"] or 0),
                stability=float(s["stability"] or 0),
                difficulty=float(s["difficulty"] or 0),
                last_review=str(s["last_review"] or ""),
            )
            values.append({"id": s["id"], "problem_id": s["problem_id"],
                           "title": s["title"], "due": s["due_date"], "r": r})
        high = sum(1 for v in values if v["r"] < 0.5)
        medium = sum(1 for v in values if 0.5 <= v["r"] < 0.7)
        avg = round(sum(v["r"] for v in values) / len(values), 3)
        top = sorted(values, key=lambda v: v["r"])[:3]
        return {"count": len(values), "high_risk": high, "medium_risk": medium,
                "avg_r": avg, "top": top}

    @staticmethod
    def _today_tasks(subject: str = "physics") -> list[dict[str, Any]]:
        """P0 今日任务清单：复习压力 + 错因专项 + 冲刺提醒。"""
        tasks: list[dict[str, Any]] = []
        pressure = ReportsMixin._pressure_index(subject)
        if pressure["total"] > 0:
            tasks.append({
                "kind": "review",
                "label": f"复习 {pressure['total']} 题（逾期 {pressure['overdue']} + 今日 {pressure['today']} + 明日 {pressure['tomorrow']}，约 {pressure['est_minutes']} 分钟）",
                "count": pressure["total"],
            })
        elif pressure["overdue"] == 0:
            tasks.append({"kind": "done", "label": "今日没有到期待复习的题目", "count": 0})
        # 错因专项：近期最高频错因 → 抽 3 道同错因题
        top_err = rows("""
            SELECT error_type, COUNT(*) AS c FROM problems
            WHERE subject = ? AND error_type <> '' AND error_type <> '待诊断'
            GROUP BY error_type ORDER BY c DESC LIMIT 1
        """, (subject,))
        if top_err:
            et = top_err[0]["error_type"]
            picks = rows("""
                SELECT title FROM problems WHERE subject = ? AND error_type = ?
                ORDER BY mastery ASC, id DESC LIMIT 3
            """, (subject, et))
            if picks:
                from errors import ERROR_TYPE_LABELS
                label = ERROR_TYPE_LABELS.get(et, et)
                tasks.append({
                    "kind": "error_focus",
                    "label": f"错因专项：「{label}」专项 3 题",
                    "count": len(picks),
                    "titles": [p["title"] for p in picks],
                })
        # 冲刺提醒
        try:
            from profile import aggregate
            goal = aggregate().get("goal", {}) or {}
            if goal.get("exam_date"):
                days = (date.fromisoformat(goal["exam_date"]) - date.today()).days
                if 0 <= days <= 14:
                    tasks.append({
                        "kind": "exam",
                        "label": f"距考试仅 {days} 天，建议按冲刺计划加练",
                        "count": days,
                    })
        except Exception:
            pass
        return tasks

    @staticmethod
    def _pressure_index(subject: str = "physics") -> dict[str, Any]:
        """P0 复习压力指数（PI）：逾期/今日/明日复习 + 预估耗时 + 压力分。"""
        today = date.today().isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        subj_sub = "(SELECT id FROM problems WHERE subject = ?)"
        overdue = row("SELECT COUNT(*) AS c FROM reviews WHERE completed = 0 AND due_date < ? AND problem_id IN " + subj_sub, (today, subject)) or {}
        today_n = row("SELECT COUNT(*) AS c FROM reviews WHERE completed = 0 AND due_date = ? AND problem_id IN " + subj_sub, (today, subject)) or {}
        tomorrow_n = row("SELECT COUNT(*) AS c FROM reviews WHERE completed = 0 AND due_date = ? AND problem_id IN " + subj_sub, (tomorrow, subject)) or {}
        overdue_c = int(overdue.get("c", 0))
        today_c = int(today_n.get("c", 0))
        tomorrow_c = int(tomorrow_n.get("c", 0))
        # 估算：每题约 90 秒（标注为估算值）
        total = overdue_c + today_c + tomorrow_c
        minutes = round(total * 1.5)
        # 压力分：逾期 2 分/题（封顶 60）+ 今日 0.8 分/题（封顶 30）+ 明日 0.3 分/题（封顶 15）
        score = min(60, overdue_c * 2) + min(30, int(today_c * 0.8)) + min(15, int(tomorrow_c * 0.3))
        if score >= 80:
            level = "高"
        elif score >= 40:
            level = "中"
        else:
            level = "低"
        return {
            "score": int(score),
            "level": level,
            "overdue": overdue_c,
            "today": today_c,
            "tomorrow": tomorrow_c,
            "total": total,
            "est_minutes": minutes,
        }

    @staticmethod
    def _error_trend(subject: str = "physics") -> list[dict[str, Any]]:
        """C7 错因趋势：近 30 天 vs 历史累计的占比对比（可指定学科）。"""
        try:
            from errors import ERROR_TYPE_LABELS
        except Exception:
            ERROR_TYPE_LABELS = {}
        month_ago = (date.today() - timedelta(days=29)).isoformat()
        recent_rows = rows("""
            SELECT error_type, COUNT(*) AS count FROM problems
            WHERE subject = ? AND error_type <> '' AND error_type <> '待诊断' AND created_at >= ?
            GROUP BY error_type
        """, (subject, month_ago))
        total_rows = rows("""
            SELECT error_type, COUNT(*) AS count FROM problems
            WHERE subject = ? AND error_type <> '' AND error_type <> '待诊断'
            GROUP BY error_type
        """, (subject,))
        recent = {r["error_type"]: r["count"] for r in recent_rows}
        total = {r["error_type"]: r["count"] for r in total_rows}
        recent_sum = max(1, sum(recent.values()))
        total_sum = max(1, sum(total.values()))
        out = []
        for etype in sorted(set(recent) | set(total), key=lambda t: -total.get(t, 0)):
            recent_count = recent.get(etype, 0)
            recent_pct = round(recent_count / recent_sum * 100, 1)
            total_pct = round(total.get(etype, 0) / total_sum * 100, 1)
            out.append({
                "type": etype,
                "label": ERROR_TYPE_LABELS.get(etype, etype),
                "recent_count": recent_count,
                "recent_pct": recent_pct,
                "total_pct": total_pct,
                "delta": round(recent_pct - total_pct, 1),
            })
        return out

    @staticmethod
    def _error_distribution(subject: str = "physics") -> list[dict[str, Any]]:
        """C6 错因分布：各错因题目数 + 平均掌握度（可指定学科）。"""
        try:
            from errors import ERROR_TYPE_LABELS
        except Exception:
            ERROR_TYPE_LABELS = {}
        dist = rows("""
            SELECT error_type AS type, COUNT(*) AS count, ROUND(AVG(mastery), 1) AS avg_mastery
            FROM problems WHERE subject = ? AND error_type <> '' AND error_type <> '待诊断'
            GROUP BY error_type ORDER BY count DESC
        """, (subject,))
        out = []
        for d in dist:
            out.append({
                "type": d["type"],
                "label": ERROR_TYPE_LABELS.get(d["type"], d["type"]),
                "count": d["count"],
                "avg_mastery": d["avg_mastery"] or 0,
            })
        pending = row("SELECT COUNT(*) AS c FROM problems WHERE subject = ? AND (error_type = '待诊断' OR error_type = '')", (subject,))
        if pending and pending["c"]:
            out.append({"type": "", "label": "待诊断", "count": pending["c"], "avg_mastery": 0})
        return out

    def _trend_data(self, subject: str = "physics") -> dict[str, Any]:
        log = rows("SELECT day, avg_mastery, count FROM mastery_log WHERE subject = ? ORDER BY id DESC LIMIT 60", (subject,))
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        summary = row("""
            SELECT COUNT(*) AS week_reviews,
                   COALESCE(ROUND(AVG(CASE WHEN CAST(result AS INTEGER) >= 3 THEN 1.0 ELSE 0 END) * 100, 0), 0) AS week_accuracy
            FROM reviews WHERE completed = 1 AND created_at >= ? AND problem_id IN (SELECT id FROM problems WHERE subject = ?)
        """, (week_ago, subject)) or {}
        week_new = row("SELECT COUNT(*) AS count FROM problems WHERE subject = ? AND created_at >= ?", (subject, week_ago))
        return {
            "points": list(reversed(log)),
            "summary": {
                "week_reviews": int(summary.get("week_reviews", 0)),
                "week_accuracy": int(summary.get("week_accuracy", 0)),
                "week_new": int(week_new["count"]) if week_new else 0,
            },
        }

    def _handle_trend(self) -> None:
        # 此前未传 subject，恒返回 physics → 切换学科后趋势数据不跟随。
        self.json_response(self._trend_data(self.subject))

    def _analytics_data(self, subject: str = "physics") -> dict[str, Any]:
        """D4 复习面展开：未来 7 天压力 / 卡组健康度 / 近 30 天复习记录。"""
        today = date.today()
        # 未来 7 天复习压力（按天）
        due_series: list[dict[str, Any]] = []
        for i in range(7):
            day = today + timedelta(days=i)
            count = row("""
                SELECT COUNT(*) AS c FROM reviews
                WHERE completed = 0 AND due_date = ? AND problem_id IN (SELECT id FROM problems WHERE subject = ?)
            """, (day.isoformat(), subject))
            due_series.append({"date": day.isoformat(), "due": count["c"] if count else 0})
        # 卡组健康度：新生(0次复习)/学习(1-2次)/成熟(3+次) 平均掌握
        health = row("""
            SELECT
                SUM(CASE WHEN repetition = 0 THEN 1 ELSE 0 END) AS newborn,
                SUM(CASE WHEN repetition BETWEEN 1 AND 2 THEN 1 ELSE 0 END) AS learning,
                SUM(CASE WHEN repetition >= 3 THEN 1 ELSE 0 END) AS mature,
                COUNT(*) AS total,
                COALESCE(ROUND(AVG(repetition), 1), 0) AS avg_repetition,
                COALESCE(ROUND(AVG(mastery), 1), 0) AS avg_mastery
            FROM problems WHERE subject = ?
        """, (subject,)) or {}
        # 最近 30 天每日复习量
        month_ago = (today - timedelta(days=29)).isoformat()
        daily = rows("""
            SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS count
            FROM reviews WHERE completed = 1 AND created_at >= ?
            AND problem_id IN (SELECT id FROM problems WHERE subject = ?)
            GROUP BY substr(created_at, 1, 10) ORDER BY day
        """, (month_ago, subject))
        return {
            "due_7d": due_series,
            "deck_health": {
                "newborn": int(health.get("newborn") or 0),
                "learning": int(health.get("learning") or 0),
                "mature": int(health.get("mature") or 0),
                "total": int(health.get("total") or 0),
                "avg_repetition": health.get("avg_repetition") or 0,
                "avg_mastery": health.get("avg_mastery") or 0,
            },
            "daily_reviews": daily,
            "forgetting": self._forgetting_curve(today, subject),
        }

    @staticmethod
    def _forgetting_curve(today: date, subject: str = "physics") -> dict[str, Any]:
        """D4 遗忘曲线：FSRS 卡按「距上次复习天数」分桶统计实际 R，并给平均稳定度下的预测曲线。"""
        cards = rows("""
            SELECT p.stability, p.difficulty, p.state, p.repetition,
                   (SELECT MAX(created_at) FROM reviews r
                    WHERE r.problem_id = p.id AND r.completed = 1) AS last_review
            FROM problems p
            WHERE p.subject = ? AND p.state > 0 AND p.stability > 0
        """, (subject,))
        buckets = [0, 4, 8, 15, 30, 61, 100000]
        labels = ["0-3天", "4-7天", "8-14天", "15-30天", "31-60天", "60天+"]
        bucket_data: list[dict[str, Any]] = []
        stability_sum = 0.0
        stability_n = 0
        for card in cards:
            ref = (card["last_review"] or "").split("T")[0].split(" ")[0]
            if not ref:
                continue
            try:
                elapsed = max(0, (today - date.fromisoformat(ref)).days)
            except ValueError:
                continue
            stability = float(card["stability"] or 0)
            stability_sum += stability
            stability_n += 1
            r = fsrs_bridge.retrievability(
                prev_interval=int(card["repetition"] or 0),
                state=int(card["state"] or 0),
                stability=stability,
                difficulty=float(card["difficulty"] or 0),
                last_review=ref,
                current=today,
            )
            idx = next((i for i, bound in enumerate(buckets) if elapsed < bound), len(buckets) - 1)
            entry = bucket_data[idx] if idx < len(bucket_data) else None
            if not entry:
                # 惰性初始化：buckets 单调递增，顺序遍历时 idx 必递增
                bucket_data.append({"label": labels[idx], "count": 0, "r_sum": 0.0, "avg_r": 0.0})
                entry = bucket_data[-1]
            entry["count"] += 1
            entry["r_sum"] += r
        for entry in bucket_data:
            entry["avg_r"] = round(entry["r_sum"] / entry["count"], 3) if entry["count"] else 0.0
        # 预测曲线：平均稳定度下 R(t) t=0..30（无卡则空）
        curve: list[dict[str, Any]] = []
        if stability_n:
            avg_s = round(stability_sum / stability_n, 2)
            for t in range(0, 31, 3):
                ref = (today - timedelta(days=t)).isoformat()
                r = fsrs_bridge.retrievability(
                    prev_interval=3, state=2, stability=avg_s,
                    difficulty=5.0, last_review=ref, current=today,
                )
                curve.append({"t": t, "r": r})
        return {"buckets": bucket_data, "curve": curve, "avg_stability": round(stability_sum / stability_n, 2) if stability_n else 0.0}

    def _handle_analytics(self) -> None:
        # 同 _handle_trend：此前忽略 ?subject=，多学科场景下恒显示 physics。
        self.json_response(self._analytics_data(self.subject))

    def _handle_gamification(self) -> None:
        from gamification import state as game_state
        self.json_response(game_state())
