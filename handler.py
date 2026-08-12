"""HTTP 请求处理器：路由分发与 JSON 响应。"""
from __future__ import annotations

import base64
import csv
import io
import json
import re
import shutil
import time
import uuid
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler
from typing import Any
from urllib.parse import urlparse, parse_qs
from pathlib import Path

from config import STATIC_DIR, LOG, DB_PATH, MEDIA_DIR
from db import DB_LOCK, db, now, row, rows, settings_dict
from ai import (
    call_ai, call_ai_stream, fallback_hint, problem_prompt, extract_tags, generate_variants,
    invalidate_settings_cache, set_runtime_key, set_master_password,
    display_settings,
)
from review import compute_review, clamp_mastery
from oral import (
    start_oral, continue_oral, draft_oral_card, start_feynman,
    feynman_self_review, save_feynman_self_review,
)
import graph
from errors import normalize_error_type, ERROR_TYPE_LABELS, is_valid_error_type
import fsrs_bridge
from fsrs_bridge import next_interval_days

# ── 写请求安全闸门（CSRF 轻量版）──
# 跨站页无法设置自定义请求头（会触发预检而本服务不响应 OPTIONS），
# 因此同源的 X-Requested-With 头即可作为写请求的合法来源证明。
X_HEADER = "X-Requested-With"
X_VALUE = "PhysicsStudyOS"

# 写幂等：客户端携带 X-Request-Id，重复提交返回首次结果，杜绝重复建题。
_IDEMPOTENCY: dict[str, tuple[int, dict[str, Any]]] = {}
_IDEMPOTENCY_TTL = 3600


def _interleave(items: list[dict[str, Any]], key: str = "topic") -> list[dict[str, Any]]:
    """A7 交错练习：按 key 分桶后贪心轮转取卡，避免同知识点连续出现。

    每步选取剩余数量最多的桶且不等于上一个桶；若只剩一个桶则按原序补完。
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


def _prune_idempotency() -> None:
    if len(_IDEMPOTENCY) < 512:
        return
    cutoff = datetime.now().timestamp() - _IDEMPOTENCY_TTL
    stale = [k for k, (ts, _) in _IDEMPOTENCY.items() if ts < cutoff]
    for k in stale:
        _IDEMPOTENCY.pop(k, None)


class Handler(SimpleHTTPRequestHandler):
    server_version = "PhysicsStudyOS/0.3.0"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def _csrf_ok(self) -> bool:
        return self.headers.get(X_HEADER) == X_VALUE

    def json_response(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("请求内容过大")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _safe_error(self, exc: Exception, status: int = 500) -> None:
        """返回通用错误消息，不暴露内部细节。"""
        if isinstance(exc, (ValueError, json.JSONDecodeError)):
            self.json_response({"error": str(exc)}, 400)
        else:
            LOG.error("请求处理异常: %s", exc, exc_info=True)
            self.json_response({"error": "服务器内部错误，请查看日志"}, status)

    # ── GET ──────────────────────────────────────────────

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/dashboard":
                self._handle_dashboard()
                return
            if path == "/api/problems":
                self._handle_list_problems()
                return
            match = re.fullmatch(r"/api/problems/(\d+)", path)
            if match:
                self._handle_get_problem(int(match.group(1)))
                return
            match = re.fullmatch(r"/api/problems/(\d+)/history", path)
            if match:
                self._handle_problem_history(int(match.group(1)))
                return
            match = re.fullmatch(r"/api/problems/(\d+)/related", path)
            if match:
                self._handle_related_problems(int(match.group(1)))
                return
            if path == "/api/problems/duplicates":
                self._handle_duplicates()
                return
            if path == "/api/reviews":
                self._handle_list_reviews()
                return
            if path == "/api/reviews/summary/today":
                self._handle_today_summary()
                return
            if path == "/api/settings":
                self.json_response(display_settings())
                return
            if path == "/api/trend":
                self._handle_trend()
                return
            if path == "/api/analytics":
                self._handle_analytics()
                return
            if path == "/api/profile":
                from profile import aggregate
                self.json_response(aggregate())
                return
            if path == "/api/graph/concepts":
                self._handle_graph()
                return
            if path == "/api/graph/problems":
                self._handle_graph_problems()
                return
            match = re.fullmatch(r"/api/feynman/(\d+)/self-review", path)
            if match:
                self._handle_feynman_self_review_get(int(match.group(1)))
                return
            match = re.fullmatch(r"/api/oral/(\d+)", path)
            if match:
                self._handle_get_oral(int(match.group(1)))
                return
            if path == "/api/export":
                self._handle_export()
                return
            if path == "/api/export/backup":
                self._handle_backup_export()
                return
            if path == "/api/ocr/probe":
                import ocr
                self.json_response({"ok": True, **ocr.probe()})
                return
            if path == "/api/health":
                self.json_response({"ok": True, "version": "0.3.0"})
                return
            if path == "/api/models/probe":
                from ai import probe_ollama
                self.json_response({"ollama": probe_ollama()})
                return
            if path == "/api/rag/docs":
                import rag
                self.json_response({"items": rag.list_docs()})
                return
            if path == "/api/rag/search":
                self._handle_rag_search()
                return
            if path == "/api/rag/open":
                self._handle_rag_open()
                return
            if path == "/api/exam/papers":
                import exam
                self.json_response(exam.overall_readiness())
                return
            match = re.fullmatch(r"/api/exam/papers/(\d+)", path)
            if match:
                import exam
                data = exam.paper_readiness(int(match.group(1)))
                if not data:
                    self.json_response({"error": "试卷不存在"}, 404)
                    return
                self.json_response(data)
                return
            if path.startswith("/media/"):
                self._serve_media(path)
                return
            super().do_GET()
        except Exception as exc:
            self._safe_error(exc)

    def _handle_list_reviews(self) -> None:
        """复习队列。?mode=interleave 时按知识点混合排序（A7 交错练习）。"""
        items = rows("""
            SELECT r.*, p.title, p.course, p.topic, p.content, p.my_attempt
            FROM reviews r JOIN problems p ON p.id = r.problem_id
            WHERE r.completed = 0 ORDER BY r.due_date ASC
        """)
        qs = parse_qs(urlparse(self.path).query)
        if qs.get("mode", [""])[0] == "interleave" and len(items) > 2:
            items = _interleave(items)
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
        self.json_response(items)

    def _handle_today_summary(self) -> None:
        """今日复盘摘要（A7）：统计今日复习结果 + 错因分布 + 明早温习建议。"""
        today = date.today().isoformat()
        done = rows("""
            SELECT r.result, p.error_type FROM reviews r JOIN problems p ON p.id = r.problem_id
            WHERE r.completed = 1 AND r.created_at >= ?
        """, (today + "T00:00:00",))
        due_tomorrow = row("""
            SELECT COUNT(*) AS count FROM reviews WHERE completed = 0 AND due_date <= ?
        """, ((date.today() + timedelta(days=1)).isoformat(),)) or {}
        done_count = len(done)
        hard = sum(1 for d in done if int(d["result"] or 0) <= 2)
        error_counts: dict[str, int] = {}
        for d in done:
            et = d["error_type"] or "待诊断"
            error_counts[et] = error_counts.get(et, 0) + 1
        top_error = sorted(error_counts.items(), key=lambda kv: -kv[1])[:1]
        top_error_label = ERROR_TYPE_LABELS.get(top_error[0][0], top_error[0][0]) if top_error else ""
        if done_count:
            accuracy = round((1 - hard / done_count) * 100)
            tip = f"今日完成 {done_count} 次复习，正确率 {accuracy}%"
            if hard:
                tip += f"，有 {hard} 次答错"
            if top_error_label:
                tip += f"，主要错因是「{top_error_label}」"
            if done_count >= 3 and accuracy >= 80:
                tip += "，保持得很好，明早可只温习 3 张旧卡。"
            else:
                tip += "，建议明早先重做今天答错的题目。"
        else:
            accuracy = 0
            tip = "今天还没有完成复习，先做一张卡吧。"
        self.json_response({
            "date": today,
            "done": done_count,
            "accuracy": accuracy,
            "due_tomorrow": int(due_tomorrow.get("count", 0)),
            "error_counts": error_counts,
            "tip": tip,
            "warmup": [3] if done_count else [],
        })

    def _handle_list_problems(self) -> None:
        """支持分页与搜索: ?page=1&limit=50&q=关键词&sort=time|mastery (limit 上限 200)"""
        qs = parse_qs(urlparse(self.path).query)
        try:
            limit = min(max(int(qs.get("limit", ["50"])[0]), 1), 200)
            page = max(int(qs.get("page", ["1"])[0]), 1)
        except (ValueError, IndexError):
            limit, page = 50, 1
        q = (qs.get("q", [""])[0] or "").strip()
        sort = qs.get("sort", ["time"])[0]
        order = {"time": "id DESC", "mastery": "mastery ASC"}.get(sort, "id DESC")
        offset = (page - 1) * limit

        if q:
            like = f"%{q}%"
            where = " WHERE title LIKE ? OR topic LIKE ? OR course LIKE ?"
            params: tuple[Any, ...] = (like, like, like)
        else:
            where, params = "", ()

        # A2 先修模式：?prereq=<concept_id> 过滤出该概念先修链上的历史错题
        prereq_param = qs.get("prereq", [""])[0]
        if prereq_param.isdigit():
            chain = graph.prereq_chain(int(prereq_param))
            if chain:
                cond = " OR ".join("concept_ids LIKE ?" for _ in chain)
                chain_params = tuple(f"%,{cid},%" for cid in chain)
                if where:
                    where += f" AND ({cond})"
                else:
                    where = f" WHERE {cond}"
                params = params + chain_params

        items = rows(
            f"SELECT id, title, course, topic, content, my_attempt, error_path, fix_action, error_type, mastery, starred, tags, tags_status, created_at, updated_at FROM problems{where} ORDER BY {order} LIMIT ? OFFSET ?",
            params + (limit, offset),
        )
        total_row = row(f"SELECT COUNT(*) AS count FROM problems{where}", params)
        total = total_row["count"] if total_row else 0
        # 一次窗口函数查询拉回所有题的最近 3 次评分（替代每道题单独查）
        if items:
            ids = [item["id"] for item in items]
            placeholders = ",".join("?" for _ in ids)
            mini_rows = rows(f"""
                SELECT problem_id, result FROM (
                    SELECT problem_id, result, id,
                        ROW_NUMBER() OVER (PARTITION BY problem_id ORDER BY id DESC) AS rn
                    FROM reviews WHERE completed = 1 AND problem_id IN ({placeholders})
                ) WHERE rn <= 3 ORDER BY problem_id, id ASC
            """, tuple(ids))
            by_pid: dict[int, list[str]] = {pid: [] for pid in ids}
            for mr in mini_rows:
                by_pid[mr["problem_id"]].append(mr["result"])
            for item in items:
                item["recent_results"] = by_pid.get(item["id"], [])
        self.json_response({
            "items": items, "total": total, "page": page, "limit": limit,
            "pages": (total + limit - 1) // limit,
        })

    def _handle_dashboard(self) -> None:
        today = date.today().isoformat()
        stats = row("""
            SELECT COUNT(*) AS total,
                   COALESCE(AVG(mastery), 0) AS avg_mastery,
                   SUM(CASE WHEN mastery >= 4 THEN 1 ELSE 0 END) AS mastered
            FROM problems
        """) or {}
        due = row("SELECT COUNT(*) AS count FROM reviews WHERE completed = 0 AND due_date <= ?", (today,))
        topics = rows("""
            SELECT topic, COUNT(*) AS count, ROUND(AVG(mastery), 1) AS mastery
            FROM problems WHERE topic <> '' GROUP BY topic ORDER BY mastery ASC, count DESC LIMIT 8
        """)
        recent = rows("SELECT id, title, course, topic, error_type, mastery, created_at, starred FROM problems ORDER BY id DESC LIMIT 5")
        recent_activity = rows("""
            SELECT r.id, r.result, r.created_at, p.id AS problem_id, p.title, p.course, p.topic
            FROM reviews r JOIN problems p ON p.id = r.problem_id
            WHERE r.completed = 1 ORDER BY r.id DESC LIMIT 5
        """)
        course_stats = rows("""
            SELECT course, COUNT(*) AS count, ROUND(AVG(mastery), 1) AS avg_mastery,
                   SUM(CASE WHEN mastery >= 4 THEN 1 ELSE 0 END) AS mastered,
                   (SELECT COUNT(*) FROM reviews WHERE completed=0 AND due_date<=? AND problem_id IN (SELECT id FROM problems p2 WHERE p2.course = p.course)) AS due
            FROM problems p WHERE course <> '' GROUP BY course ORDER BY avg_mastery ASC LIMIT 6
        """, (today,))
        # C6 合并仪表盘数据：趋势 / 分析 / 错因分布（单请求）
        trend = self._trend_data()
        analytics = self._analytics_data()
        self.json_response({
            "stats": stats, "due": due["count"] if due else 0, "topics": topics,
            "recent": recent, "recent_activity": recent_activity, "course_stats": course_stats,
            "points": trend["points"], "summary": trend["summary"],
            "due_7d": analytics["due_7d"], "deck_health": analytics["deck_health"],
            "daily_reviews": analytics["daily_reviews"],
            "error_distribution": self._error_distribution(),
            "error_trend": self._error_trend(),
        })

    @staticmethod
    def _error_trend() -> list[dict[str, Any]]:
        """C7 错因趋势：近 30 天新增 vs 历史累计的占比对比（↑恶化 ↓改善）。"""
        try:
            from errors import ERROR_TYPE_LABELS
        except Exception:
            ERROR_TYPE_LABELS = {}
        month_ago = (date.today() - timedelta(days=29)).isoformat()
        recent_rows = rows("""
            SELECT error_type, COUNT(*) AS count FROM problems
            WHERE error_type <> '' AND error_type <> '待诊断' AND created_at >= ?
            GROUP BY error_type
        """, (month_ago,))
        total_rows = rows("""
            SELECT error_type, COUNT(*) AS count FROM problems
            WHERE error_type <> '' AND error_type <> '待诊断'
            GROUP BY error_type
        """)
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
    def _similarity(a: str, b: str) -> float:
        """C7 字符 bigram Jaccard 相似度（零依赖，中文/英文通用）。"""
        a = re.sub(r"\s+", "", a or "")
        b = re.sub(r"\s+", "", b or "")
        if not a or not b:
            return 0.0
        def bigrams(s: str) -> set[str]:
            return {s[i:i + 2] for i in range(max(0, len(s) - 1))} if len(s) > 1 else {s}
        ba, bb = bigrams(a), bigrams(b)
        if not ba and not bb:
            return 1.0
        union = ba | bb
        return len(ba & bb) / len(union) if union else 0.0

    def _handle_duplicates(self) -> None:
        """C7 查重：按 topic 加权 + 内容 bigram 相似度，返回 top 相似题。"""
        qs = parse_qs(urlparse(self.path).query)
        content = (qs.get("content", [""])[0] or "").strip()
        topic = (qs.get("topic", [""])[0] or "").strip()
        exclude = qs.get("exclude", [""])[0]
        if not content:
            self.json_response({"duplicates": []})
            return
        candidates = rows("SELECT id, title, topic, content FROM problems WHERE id <> ? ORDER BY id DESC LIMIT 300",
                          (exclude or 0,))
        scored = []
        for c in candidates:
            sim = self._similarity(content, c["content"])
            if topic and topic == c["topic"]:
                sim = min(1.0, sim + 0.15)
            if sim >= 0.35:
                scored.append({
                    "id": c["id"], "title": c["title"], "topic": c["topic"],
                    "similarity": round(sim, 2),
                })
        scored.sort(key=lambda x: -x["similarity"])
        self.json_response({"duplicates": scored[:5]})

    @staticmethod
    def _error_distribution() -> list[dict[str, Any]]:
        """C6 错因分布：各错因计数与平均掌握度（带中文标签）。"""
        try:
            from errors import ERROR_TYPE_LABELS
        except Exception:
            ERROR_TYPE_LABELS = {}
        dist = rows("""
            SELECT error_type AS type, COUNT(*) AS count, ROUND(AVG(mastery), 1) AS avg_mastery
            FROM problems WHERE error_type <> '' AND error_type <> '待诊断'
            GROUP BY error_type ORDER BY count DESC
        """)
        out = []
        for d in dist:
            out.append({
                "type": d["type"],
                "label": ERROR_TYPE_LABELS.get(d["type"], d["type"]),
                "count": d["count"],
                "avg_mastery": d["avg_mastery"] or 0,
            })
        pending = row("SELECT COUNT(*) AS c FROM problems WHERE error_type = '待诊断' OR error_type = ''")
        if pending and pending["c"]:
            out.append({"type": "", "label": "待诊断", "count": pending["c"], "avg_mastery": 0})
        return out

    def _trend_data(self) -> dict[str, Any]:
        log = rows("SELECT day, avg_mastery, count FROM mastery_log ORDER BY id DESC LIMIT 60")
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        summary = row("""
            SELECT COUNT(*) AS week_reviews,
                   COALESCE(ROUND(AVG(CASE WHEN CAST(result AS INTEGER) >= 3 THEN 1.0 ELSE 0 END) * 100, 0), 0) AS week_accuracy
            FROM reviews WHERE completed = 1 AND created_at >= ?
        """, (week_ago,)) or {}
        week_new = row("SELECT COUNT(*) AS count FROM problems WHERE created_at >= ?", (week_ago,))
        return {
            "points": list(reversed(log)),
            "summary": {
                "week_reviews": int(summary.get("week_reviews", 0)),
                "week_accuracy": int(summary.get("week_accuracy", 0)),
                "week_new": int(week_new["count"]) if week_new else 0,
            },
        }

    def _handle_trend(self) -> None:
        self.json_response(self._trend_data())

    def _analytics_data(self) -> dict[str, Any]:
        """D4 复习分析扩展：未来 7 天压力 / 卡组健康度 / 最近 30 天复习量。"""
        today = date.today()
        # 未来 7 天复习压力（含今日）
        due_series: list[dict[str, Any]] = []
        for i in range(7):
            day = today + timedelta(days=i)
            count = row("""
                SELECT COUNT(*) AS c FROM reviews
                WHERE completed = 0 AND due_date = ?
            """, (day.isoformat(),))
            due_series.append({"date": day.isoformat(), "due": count["c"] if count else 0})
        # 卡组健康度：新生(0次复习)/学习(1-2次)/成长(3+次) 与平均间隔
        health = row("""
            SELECT
                SUM(CASE WHEN repetition = 0 THEN 1 ELSE 0 END) AS newborn,
                SUM(CASE WHEN repetition BETWEEN 1 AND 2 THEN 1 ELSE 0 END) AS learning,
                SUM(CASE WHEN repetition >= 3 THEN 1 ELSE 0 END) AS mature,
                COUNT(*) AS total,
                COALESCE(ROUND(AVG(repetition), 1), 0) AS avg_repetition,
                COALESCE(ROUND(AVG(mastery), 1), 0) AS avg_mastery
            FROM problems
        """) or {}
        # 最近 30 天每日复习量
        month_ago = (today - timedelta(days=29)).isoformat()
        daily = rows("""
            SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS count
            FROM reviews WHERE completed = 1 AND created_at >= ?
            GROUP BY substr(created_at, 1, 10) ORDER BY day
        """, (month_ago,))
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
        }

    def _handle_analytics(self) -> None:
        self.json_response(self._analytics_data())

    def _handle_get_oral(self, session_id: int) -> None:
        """返回一次口试会话的完整 transcript。"""
        item = row("SELECT * FROM oral_sessions WHERE id = ?", (session_id,))
        if not item:
            self.json_response({"error": "口试会话不存在"}, 404)
            return
        item["transcript"] = json.loads(item["transcript"]) if item["transcript"] else []
        self.json_response(item)

    def _handle_ocr_extract(self, data) -> None:
        """OCR 提取：工作区内 PDF/图片 → 文本。"""
        raw = str((data or {}).get("path", "")).strip()
        if not raw:
            self.json_response({"error": "缺少 path"}, 400)
            return
        from rag import _safe_relative
        fp = _safe_relative(raw)
        if not fp:
            self.json_response({"error": "路径必须在工作区内"}, 400)
            return
        if not fp.is_file():
            self.json_response({"error": f"文件不存在: {raw}"}, 400)
            return
        try:
            import ocr
            result = ocr.extract_pdf(fp) if fp.suffix.lower() == ".pdf" else ocr.extract_image(fp)
            result["path"] = str(fp)
            self.json_response({"ok": True, **result})
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 400)

    def _handle_export(self) -> None:
        """只读导出。?format=json|anki-csv|ics（默认 json）。"""
        qs = parse_qs(urlparse(self.path).query)
        fmt = (qs.get("format", ["json"])[0] or "json").strip()
        if fmt == "anki-csv":
            self._export_anki_csv()
            return
        if fmt == "ics":
            self._export_ics()
            return
        problems = rows("SELECT id, title, course, topic, content, my_attempt, error_type, error_path, trap_note, shortcut, fix_action, tags, tags_status, mastery, created_at, updated_at FROM problems ORDER BY id")
        for p in problems:
            try:
                p["tags"] = json.loads(p["tags"]) if p["tags"] else []
            except (json.JSONDecodeError, TypeError):
                p["tags"] = []
        data = {
            "version": 1,
            "exported_at": now(),
            "problems": problems,
            "hints": rows("SELECT problem_id, level, content, created_at FROM hints ORDER BY id"),
            "reviews": rows("SELECT problem_id, due_date, interval_days, result, completed, created_at FROM reviews ORDER BY id"),
        }
        self.json_response(data)

    def _handle_backup_export(self) -> None:
        """一键备份：全库 JSON 下载。"""
        import backup as backup_mod
        data = backup_mod.export_backup()
        body = json.dumps(data, ensure_ascii=False, indent=1)
        self._text_response(
            body,
            "application/json",
            f"physics-study-backup-{time.strftime('%Y%m%d-%H%M%S')}.json",
        )

    def _handle_backup_restore(self, data) -> None:
        """一键还原：接收备份 JSON，工作区内重建库。"""
        raw = data.get("backup") if isinstance(data, dict) else None
        if not isinstance(raw, str) or not raw.strip():
            self.json_response({"error": "缺少 backup 字段"}, 400)
            return
        try:
            import backup as backup_mod
            result = backup_mod.restore_backup(raw)
            self.json_response({"ok": True, **result})
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 400)

    def _text_response(self, body: str, content_type: str, filename: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _export_anki_csv(self) -> None:
        """Anki 导入 CSV：question|answer|tags（UTF-8 BOM，兼容 Anki 桌面端）。"""
        problems = rows("""
            SELECT p.id, p.title, p.course, p.topic, p.content, p.my_attempt, p.tags,
                   p.error_path, p.trap_note, p.shortcut, p.fix_action,
                   (SELECT GROUP_CONCAT(content, '\n') FROM hints h WHERE h.problem_id = p.id AND h.level = 3) AS answer_hint
            FROM problems p ORDER BY p.id
        """)
        buf = io.StringIO()
        writer = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\n")
        for p in problems:
            front = f"{p['title']}\n{p['content']}".strip()
            back_parts = []
            if p.get("my_attempt"):
                back_parts.append(f"我的尝试：{p['my_attempt']}")
            if p.get("answer_hint"):
                back_parts.append(f"解题框架：{p['answer_hint']}")
            if p.get("shortcut"):
                back_parts.append(f"捷径：{p['shortcut']}")
            if p.get("fix_action"):
                back_parts.append(f"改进：{p['fix_action']}")
            back = "\n".join(back_parts) or "（无解析）"
            tags = " ".join(t for t in [p["course"], p["topic"]] if t)
            try:
                tag_list = json.loads(p.get("tags") or "[]")
                if isinstance(tag_list, list):
                    tags = " ".join(str(t).replace(":", "_") for t in tag_list if str(t).strip())
            except json.JSONDecodeError:
                pass
            writer.writerow([front, back, tags])
        self._text_response("\ufeff" + buf.getvalue(), "text/csv; charset=utf-8", "physics_study_anki.csv")

    def _export_ics(self) -> None:
        """复习日程 .ics：未完成的 due 复习任务导出为 VEVENT。"""
        due = rows("SELECT r.id, r.due_date, r.interval_days, p.title FROM reviews r JOIN problems p ON p.id = r.problem_id WHERE r.completed = 0 ORDER BY r.due_date")
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//PhysicsStudyOS//CN",
            "CALSCALE:GREGORIAN",
            "X-WR-CALNAME:物理复习日程",
        ]
        for r in due:
            uid = f"pso-review-{r['id']}@physics-study-os"
            stamp = r["due_date"].replace("-", "") + "T090000"
            summary = f"复习：{r['title']}"
            lines += [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTART:{stamp}",
                f"DTEND:{stamp}",
                f"SUMMARY:{summary}",
                "END:VEVENT",
            ]
        lines.append("END:VCALENDAR")
        self._text_response("\r\n".join(lines), "text/calendar; charset=utf-8", "physics_study_review.ics")

    def _handle_get_problem(self, problem_id: int) -> None:
        item = row("SELECT * FROM problems WHERE id = ?", (problem_id,))
        if not item:
            self.json_response({"error": "题目不存在"}, 404)
            return
        try:
            item["tags"] = json.loads(item["tags"]) if item["tags"] else []
        except (json.JSONDecodeError, TypeError):
            item["tags"] = []
        try:
            item["variants"] = json.loads(item["variants"]) if item["variants"] else []
        except (json.JSONDecodeError, TypeError):
            item["variants"] = []
        # A2：concept_ids 解析 + 先修掌握度告警
        item["concept_ids"] = graph.concept_ids_to_list(item.get("concept_ids") or "")
        item["prereq_warnings"] = graph.prereq_warnings(problem_id)
        # A5：Feynman 自评表（已保存的最新一条）
        feynman = row(
            "SELECT self_review FROM oral_sessions "
            "WHERE problem_id = ? AND mode = 'feynman' AND self_review != '' "
            "ORDER BY id DESC LIMIT 1",
            (problem_id,),
        )
        if feynman:
            try:
                item["feynman_self_review"] = json.loads(feynman["self_review"])
            except json.JSONDecodeError:
                item["feynman_self_review"] = None
        else:
            item["feynman_self_review"] = None
        # B1：图片附件列表
        item["media_list"] = [p for p in (item.get("media_path") or "").split(",") if p.strip()]
        item["hints"] = rows("SELECT level, content, created_at FROM hints WHERE problem_id = ? ORDER BY level", (problem_id,))
        self.json_response(item)

    def _handle_problem_history(self, problem_id: int) -> None:
        """一道题的全部已完成复习记录（SM-2 轨迹）。"""
        history = rows("""
            SELECT due_date, result, interval_days, created_at
            FROM reviews WHERE problem_id = ? AND completed = 1
            ORDER BY id ASC
        """, (problem_id,))
        self.json_response(history)

    def _handle_related_problems(self, problem_id: int) -> None:
        """同知识点 / 同课程的其他题目（排除自身，最多 3 题）。"""
        p = row("SELECT topic, course FROM problems WHERE id = ?", (problem_id,))
        if not p:
            self.json_response({"error": "题目不存在"}, 404)
            return
        topic = p["topic"] or ""
        course = p["course"] or ""
        related = rows(
            "SELECT id, title, course, topic, mastery FROM problems WHERE id != ? AND (topic = ? OR course = ?) ORDER BY id DESC LIMIT 3",
            (problem_id, topic, course),
        )
        self.json_response(related)

    def _handle_graph(self) -> None:
        """A2：全图谱（节点含掌握度，边含关系）。"""
        graph.update_progress()
        data = graph.load_graph()
        # 附加绑定题数（looms_in）
        bound = rows("""
            SELECT c.concept_id AS cid, COUNT(*) AS c FROM concept_progress c GROUP BY c.concept_id
        """)
        bound_map = {int(b["cid"]): int(b["c"]) for b in bound}
        for node in data["nodes"]:
            node["looms_in"] = bound_map.get(int(node["id"]), 0)
            prog = node.get("mastery_est") or 0.0
            node["mastery_est"] = round(prog, 3)
        self.json_response(data)

    def _handle_graph_problems(self) -> None:
        """A2 先修模式：?concept=<id> 返回该概念先修链上的相关错题。"""
        qs = parse_qs(urlparse(self.path).query)
        cid = qs.get("concept", [""])[0]
        if not cid.isdigit():
            self.json_response({"error": "缺少 concept 参数"}, 400)
            return
        chain = graph.prereq_chain(int(cid))
        items = graph.problems_for_concepts(chain)
        self.json_response({"items": items, "chain_count": len(chain)})

    def _handle_graph_add(self, data: dict[str, Any]) -> None:
        """A2：用户新增概念（user_edited=1，改动仅存库不回写 seed）。"""
        cid = graph.add_concept(str(data.get("name", "")), int(data.get("parent_id", 0) or 0))
        if not cid:
            self.json_response({"error": "概念名不能为空或已存在"}, 400)
            return
        graph.update_progress()
        self.json_response({"id": cid})

    # ── POST ─────────────────────────────────────────────

    def do_POST(self) -> None:
        if not self._csrf_ok():
            self.json_response({"error": "请求来源不被信任 (缺少 X-Requested-With)"}, 403)
            return
        path = urlparse(self.path).path
        try:
            data = self.read_json()
            if path == "/api/problems":
                self._handle_create_problem(data)
                return
            match = re.fullmatch(r"/api/problems/(\d+)/hint", path)
            if match:
                self._handle_hint(int(match.group(1)), data)
                return
            match = re.fullmatch(r"/api/problems/(\d+)/variants/generate", path)
            if match:
                self._handle_generate_variants(int(match.group(1)))
                return
            match = re.fullmatch(r"/api/problems/(\d+)/variants", path)
            if match:
                self._handle_save_variants(int(match.group(1)), data)
                return
            match = re.fullmatch(r"/api/reviews/(\d+)/complete", path)
            if match:
                self._handle_complete_review(int(match.group(1)), data)
                return
            match = re.fullmatch(r"/api/oral/(\d+)/end", path)
            if match:
                self._handle_oral_end(int(match.group(1)))
                return
            match = re.fullmatch(r"/api/oral/(\d+)/draft-card", path)
            if match:
                self._handle_oral_draft_card(int(match.group(1)))
                return
            match = re.fullmatch(r"/api/feynman/(\d+)/self-review", path)
            if match:
                self._handle_feynman_self_review(int(match.group(1)), data)
                return
            if path == "/api/feynman/start":
                self._handle_feynman_start(data)
                return
            if path == "/api/upload/photo":
                self._handle_upload_photo(data)
                return
            if path == "/api/ai/extract-photo":
                self._handle_extract_photo(data)
                return
            if path == "/api/rag/ingest":
                self._handle_rag_ingest(data)
                return
            if path == "/api/exam/papers":
                import exam
                name = str(data.get("name", "")).strip()
                if not name:
                    self.json_response({"error": "试卷名称不能为空"}, 400)
                    return
                pid = exam.create_paper(name, str(data.get("exam_date", "")).strip(),
                                        float(data.get("target", 80) or 80))
                self.json_response({"id": pid}, 201)
                return
            match = re.fullmatch(r"/api/exam/papers/(\d+)/questions", path)
            if match:
                import exam
                paper_id = int(match.group(1))
                if not row("SELECT 1 FROM exam_papers WHERE id = ?", (paper_id,)):
                    self.json_response({"error": "试卷不存在"}, 404)
                    return
                count = exam.add_questions(paper_id, data.get("questions") or [])
                self.json_response({"added": count}, 201)
                return
            if path == "/api/graph/concepts":
                self._handle_graph_add(data)
                return
            if path == "/api/ocr/extract":
                self._handle_ocr_extract(data)
                return
            if path == "/api/oral/start":
                self._handle_oral_start(data)
                return
            if path == "/api/oral/respond":
                self._handle_oral_respond(data)
                return
            if path == "/api/import":
                self._handle_import(data)
                return
            if path == "/api/import/restore":
                self._handle_backup_restore(data)
                return
            if path == "/api/ai/extract-tags":
                self._handle_extract_tags(data)
                return
            if path == "/api/problems/batch":
                self._handle_batch(data)
                return
            if path == "/api/settings/test":
                reply = call_ai([
                    {"role": "system", "content": "只回答：连接成功"},
                    {"role": "user", "content": "测试连接"},
                ], max_tokens=20)
                self.json_response({"ok": True, "reply": reply})
                return
            self.json_response({"error": "接口不存在"}, 404)
        except Exception as exc:
            self._safe_error(exc)

    def _handle_create_problem(self, data: dict[str, Any]) -> None:
        rid = self.headers.get("X-Request-Id")
        if rid and rid in _IDEMPOTENCY:
            ts, cached = _IDEMPOTENCY[rid]
            if ts >= datetime.now().timestamp() - _IDEMPOTENCY_TTL:
                self.json_response(cached, 201)
                return
            _IDEMPOTENCY.pop(rid, None)

        title = str(data.get("title", "")).strip()
        content = str(data.get("content", "")).strip()
        if not title or not content:
            self.json_response({"error": "标题和题目内容不能为空"}, 400)
            return
        stamp = now()
        error_type = normalize_error_type(data.get("error_type", "待诊断"))
        tags = json.dumps(data.get("tags", []), ensure_ascii=False)
        tags_status = "confirmed" if data.get("tags") else "none"
        # A2：显式 concept_ids 校验存在后落库；未提供则稍后自动绑定
        raw_concepts = data.get("concept_ids") or []
        if isinstance(raw_concepts, list):
            concept_csv = ",".join(f",{cid}," for cid in raw_concepts if isinstance(cid, int)) or ""
        else:
            concept_csv = ""
        with DB_LOCK, db() as conn:
            cursor = conn.execute("""
                INSERT INTO problems(title, course, topic, content, my_attempt, error_type,
                                     error_path, trap_note, shortcut, fix_action, tags, tags_status,
                                     concept_ids, media_path, mastery, ease_factor, repetition,
                                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 2.5, 0, ?, ?)
            """, (
                title, str(data.get("course", "")).strip(), str(data.get("topic", "")).strip(),
                content, str(data.get("my_attempt", "")).strip(), error_type,
                str(data.get("error_path", "")).strip(), str(data.get("trap_note", "")).strip(),
                str(data.get("shortcut", "")).strip(), str(data.get("fix_action", "")).strip(),
                tags, tags_status, concept_csv,
                self._normalize_media_paths(data.get("media_path", "")),
                clamp_mastery(int(data.get("mastery", 1))), stamp, stamp,
            ))
            problem_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO reviews(problem_id, due_date, interval_days, created_at) VALUES (?, ?, 1, ?)",
                (problem_id, (date.today() + timedelta(days=1)).isoformat(), stamp),
            )
        if not concept_csv:
            graph.bind_problem(problem_id)
        result = {"id": problem_id}
        if rid:
            _IDEMPOTENCY[rid] = (datetime.now().timestamp(), result)
            _prune_idempotency()
        self.json_response(result, 201)

    def _rag_context(self, problem: dict[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
        """B3：检索个人资料（教材/笔记）相关片段，注入 AI 上下文；返回 (注入消息, 溯源列表)。"""
        try:
            import rag
            hits = rag.search(f"{problem.get('topic', '')} {problem.get('title', '')} "
                              f"{str(problem.get('content', ''))[:200]}", k=2)
        except Exception:
            return [], []
        docs = {d["id"]: d for d in rag.list_docs()}
        sources: list[dict[str, Any]] = []
        frags: list[str] = []
        for hit in hits:
            doc = docs.get(hit["doc_id"])
            if not doc:
                continue
            page = hit.get("page") or 0
            src = {"path": doc["source_path"], "page": page,
                   "name": Path(doc["source_path"]).name}
            sources.append(src)
            frags.append(f"[{src['name']}" + (f" 第{page}页" if page else "") + f"] {hit['content']}")
        if not frags:
            return [], []
        return [{"role": "system", "content": (
            "以下是用户个人资料（教材/课件/笔记）中检索到的相关片段，"
            "解答时应优先基于这些片段给出与教材一致的表述：\n" + "\n".join(frags)
        )}], sources

    def _handle_hint(self, problem_id: int, data: dict[str, Any]) -> None:
        level = max(1, min(4, int(data.get("level", 1))))
        problem = row("SELECT * FROM problems WHERE id = ?", (problem_id,))
        if not problem:
            self.json_response({"error": "题目不存在"}, 404)
            return
        # A6 诊断门：最近一次复习失败（忘记/模糊）时，一级提示附加诊断建议
        diagnose = False
        last_result = row("""
            SELECT result FROM reviews WHERE problem_id = ? AND completed = 1
            ORDER BY id DESC LIMIT 1
        """, (problem_id,))
        if last_result and last_result["result"].isdigit() and int(last_result["result"]) <= 2:
            diagnose = True
        existing = row("SELECT content FROM hints WHERE problem_id = ? AND level = ?", (problem_id, level))
        if existing:
            self.json_response({"content": existing["content"], "source": "saved", "diagnose": diagnose})
            return
        rag_messages, rag_sources = self._rag_context(problem)
        if self._wants_sse():
            self._stream_hint(problem, level, diagnose, rag_messages, rag_sources)
            return
        source = "ai"
        try:
            hint = call_ai(problem_prompt(problem, level) + rag_messages, tier="fast")
        except Exception as exc:
            hint = fallback_hint(problem, level)
            source = "fallback"
            LOG.warning("提示降级 (problem=%s, level=%d): %s", problem_id, level, exc)
        with DB_LOCK, db() as conn:
            conn.execute(
                "INSERT INTO hints(problem_id, level, content, created_at) VALUES (?, ?, ?, ?)",
                (problem_id, level, hint, now()),
            )
        self.json_response({"content": hint, "source": source, "diagnose": diagnose, "sources": rag_sources})

    def _wants_sse(self) -> bool:
        accept = self.headers.get("Accept", "")
        return "text/event-stream" in accept

    def _sse_send(self, event: str, payload: dict[str, Any]) -> None:
        line = f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        self.wfile.write(line.encode("utf-8"))
        self.wfile.flush()

    def _stream_hint(self, problem: dict[str, Any], level: int, diagnose: bool = False,
                     rag_messages: list[dict[str, str]] | None = None,
                     rag_sources: list[dict[str, Any]] | None = None) -> None:
        """SSE 流式提示：start → delta* → done | error（含 partial）。"""
        rag_messages = rag_messages or []
        rag_sources = rag_sources or []
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self._sse_send("start", {"problem_id": problem["id"], "level": level})
        if rag_sources:
            self._sse_send("sources", {"sources": rag_sources})
        collected: list[str] = []
        try:
            chunks = call_ai_stream(problem_prompt(problem, level) + rag_messages, tier="fast")
            for delta in chunks:
                collected.append(delta)
                self._sse_send("delta", {"delta": delta})
            hint = "".join(collected).strip()
            if not hint:
                raise RuntimeError("AI 流式返回为空")
            with DB_LOCK, db() as conn:
                conn.execute(
                    "INSERT INTO hints(problem_id, level, content, created_at) VALUES (?, ?, ?, ?)",
                    (problem["id"], level, hint, now()),
                )
            self._sse_send("done", {"content": hint, "source": "ai", "diagnose": diagnose,
                                    "sources": rag_sources})
        except Exception as exc:
            LOG.warning("流式提示降级 (problem=%s, level=%d): %s", problem["id"], level, exc)
            fallback = fallback_hint(problem, level)
            try:
                with DB_LOCK, db() as conn:
                    conn.execute(
                        "INSERT INTO hints(problem_id, level, content, created_at) VALUES (?, ?, ?, ?)",
                        (problem["id"], level, fallback, now()),
                    )
            except Exception:
                pass
            self._sse_send("error", {
                "partial": "".join(collected),
                "fallback": fallback,
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
            self._log_mastery(conn)
        self.json_response({"next_due": next_due, "interval_days": result.interval_days})

    @staticmethod
    def _log_variant_result(conn: Any, review: dict[str, Any], rating: int) -> None:
        """A4：变式复习后回写题根质量分（correct/total），低正确率变式自动降权。"""
        vid = int(review.get("variant_id") or 0)
        if not vid:
            return
        p = conn.execute("SELECT variants FROM problems WHERE id = ?", (review["problem_id"],)).fetchone()
        if not p or not p["variants"]:
            return
        try:
            variants = json.loads(p["variants"])
        except json.JSONDecodeError:
            return
        idx = vid - 1
        if not (0 <= idx < len(variants)):
            return
        v = variants[idx]
        v["correct"] = int(v.get("correct", 0)) + (1 if rating >= 3 else 0)
        v["total"] = int(v.get("total", 0)) + 1
        conn.execute("UPDATE problems SET variants = ? WHERE id = ?",
                     (json.dumps(variants, ensure_ascii=False), review["problem_id"]))

    @staticmethod
    def _log_mastery(conn: Any) -> None:
        """记录今日掌握度均值（每天保留最新一条），用于趋势图。"""
        r = conn.execute("SELECT AVG(mastery) AS a, COUNT(*) AS c FROM problems").fetchone()
        avg = round(r["a"] or 0, 2)
        today = date.today().isoformat()
        conn.execute("DELETE FROM mastery_log WHERE day = ?", (today,))
        conn.execute(
            "INSERT INTO mastery_log(day, avg_mastery, count) VALUES (?, ?, ?)",
            (today, avg, r["c"] or 0),
        )

    def _handle_oral_start(self, data: dict[str, Any]) -> None:
        topic = str(data.get("topic", "")).strip()
        if not topic:
            self.json_response({"error": "请输入口试主题"}, 400)
            return
        session_id, question = start_oral(topic)
        self.json_response({"session_id": session_id, "reply": question})

    def _handle_oral_respond(self, data: dict[str, Any]) -> None:
        session_id = int(data.get("session_id", 0))
        answer = str(data.get("answer", "")).strip()
        session = row("SELECT * FROM oral_sessions WHERE id = ?", (session_id,))
        if not session or not answer:
            self.json_response({"error": "口试会话或回答无效"}, 400)
            return
        reply = continue_oral(session, answer)
        self.json_response({"reply": reply, "finished": "【口试结束】" in reply})

    def _handle_oral_draft_card(self, session_id: int) -> None:
        """F1 流水线：口试 → 复习卡草稿（R3 只返回草稿，前端确认后走创建端点）。"""
        session = row("SELECT * FROM oral_sessions WHERE id = ?", (session_id,))
        if not session:
            self.json_response({"error": "口试会话不存在"}, 404)
            return
        self.json_response({"draft": draft_oral_card(session)})

    def _handle_feynman_start(self, data: dict[str, Any]) -> None:
        """A5：对错题启动 Feynman 口述反转。"""
        problem = row("SELECT * FROM problems WHERE id = ?", (int(data.get("problem_id", 0) or 0),))
        if not problem:
            self.json_response({"error": "题目不存在"}, 404)
            return
        session_id, question = start_feynman(problem)
        self.json_response({"session_id": session_id, "reply": question})

    def _handle_feynman_self_review(self, session_id: int, data: dict[str, Any]) -> None:
        """A5：自评表草稿（GET 语义由 GET 端点承担，此 POST 为确认落库，R3）。"""
        if not save_feynman_self_review(session_id, data):
            self.json_response({"error": "自评表不能为空"}, 400)
            return
        self.json_response({"ok": True})

    def _handle_feynman_self_review_get(self, session_id: int) -> None:
        """A5：自评表（未保存返回草稿，已保存返回正式表）。"""
        session = row("SELECT * FROM oral_sessions WHERE id = ?", (session_id,))
        if not session:
            self.json_response({"error": "口试会话不存在"}, 404)
            return
        self.json_response(feynman_self_review(session))

    def _handle_oral_end(self, session_id: int) -> None:
        """结束口试会话，将状态设为 finished。"""
        session = row("SELECT * FROM oral_sessions WHERE id = ?", (session_id,))
        if not session:
            self.json_response({"error": "口试会话不存在"}, 404)
            return
        with DB_LOCK, db() as conn:
            conn.execute("UPDATE oral_sessions SET status = 'finished' WHERE id = ?", (session_id,))
        self.json_response({"ok": True})

    # ── B1 拍照/截图录题 ────────────────────────────────
    _PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
    _JPEG_MAGIC = b"\xff\xd8\xff"

    @staticmethod
    def _image_ext(blob: bytes) -> str:
        if blob.startswith(Handler._PNG_MAGIC):
            return "png"
        if blob.startswith(Handler._JPEG_MAGIC):
            return "jpg"
        return ""

    @staticmethod
    def _normalize_media_paths(value: Any) -> str:
        """拼接图片相对路径（逗号分隔，去空去重，仅允许 media/ 前缀）。"""
        if isinstance(value, list):
            parts = [str(p).strip() for p in value]
        else:
            parts = str(value or "").split(",")
        seen: list[str] = []
        for p in parts:
            p = p.strip().replace("\\", "/")
            if p.startswith("media/") and p not in seen:
                seen.append(p)
        return ",".join(seen)

    @staticmethod
    def _media_file(rel: str) -> Path | None:
        """校验 media/ 相对路径并返回工作区内文件路径（防目录穿越）。"""
        rel = str(rel or "").strip().replace("\\", "/")
        if not rel.startswith("media/"):
            return None
        fp = (MEDIA_DIR.parent / rel).resolve()
        if MEDIA_DIR.resolve() not in fp.parents and fp.parent != MEDIA_DIR.resolve():
            return None
        return fp if fp.is_file() else None

    def _handle_upload_photo(self, data: dict[str, Any]) -> None:
        """B1：上传截图/照片到 media/（魔数校验 + 大小限制），返回相对路径。"""
        raw = str(data.get("data", "")).strip()
        if not raw:
            raise ValueError("缺少图片数据")
        try:
            blob = base64.b64decode(raw, validate=True)
        except Exception:
            raise ValueError("图片数据不是合法的 base64")
        if not blob:
            raise ValueError("图片为空")
        if len(blob) > 8 * 1024 * 1024:
            raise ValueError("图片过大（上限 8MB）")
        ext = self._image_ext(blob)
        if not ext:
            raise ValueError("仅支持 PNG/JPEG 图片")
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"{int(time.time())}_{uuid.uuid4().hex[:8]}.{ext}"
        (MEDIA_DIR / fname).write_bytes(blob)
        rel = f"media/{fname}"
        self.json_response({"path": rel, "url": f"/{rel}"})

    def _handle_extract_photo(self, data: dict[str, Any]) -> None:
        """B1：视觉模型识别题目 → 卡片草稿（R3 不落库）；无 vision 降级为纯附件。"""
        fp = self._media_file(str(data.get("media_path", "")).strip())
        if not fp:
            raise ValueError("图片不存在")
        blob = fp.read_bytes()
        mime = "image/png" if self._image_ext(blob) == "png" else "image/jpeg"
        uri = f"data:{mime};base64," + base64.b64encode(blob).decode("ascii")
        try:
            from ai import call_ai_vision
            raw = call_ai_vision(
                "请识别图片中的物理题目并输出 JSON（只输出 JSON，不要其它文字）："
                '{"title": "题目概要（一句话）", "content": "完整题干与选项", '
                '"options": ["A. ...", "B. ..."], "answer": "正确答案", '
                '"analysis": "解析要点", "topic": "所属知识点"}',
                uri,
            )
            draft = json.loads(raw)
            if not isinstance(draft, dict):
                raise ValueError("识别结果格式错误")
            self.json_response({"draft": {
                "title": str(draft.get("title", "")).strip(),
                "content": str(draft.get("content", "")).strip(),
                "options": draft.get("options", []),
                "answer": str(draft.get("answer", "")).strip(),
                "analysis": str(draft.get("analysis", "")).strip(),
                "topic": str(draft.get("topic", "")).strip(),
            }})
        except ValueError as exc:
            # 未配置 vision 模型：图片仅作附件 + 手动录入（方案降级路径）
            LOG.warning("视觉识别不可用，降级为附件模式: %s", exc)
            self.json_response({"draft": None, "degraded": True, "error": str(exc)})

    def _handle_rag_ingest(self, data: dict[str, Any]) -> None:
        """B3：摄取工作区内教材/课件/笔记（文件或目录）。"""
        import rag
        path = str(data.get("path", "")).strip()
        if not path:
            self.json_response({"error": "请提供工作区内路径（文件或目录）"}, 400)
            return
        try:
            stats = rag.ingest_path(path)
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 400)
            return
        self.json_response({"ok": True, **stats})

    def _handle_rag_search(self) -> None:
        """B3：BM25（+可选 FTS5）检索，返回带溯源的结果。"""
        qs = parse_qs(urlparse(self.path).query)
        q = qs.get("q", [""])[0].strip()
        if not q:
            self.json_response({"items": [], "error": "缺少查询词"}, 400)
            return
        import rag
        k = min(10, max(1, int(qs.get("k", ["5"])[0] or 5)))
        items = rag.search(q, k)
        docs = {d["id"]: d for d in rag.list_docs()}
        for item in items:
            doc = docs.get(item["doc_id"])
            item["source_path"] = doc["source_path"] if doc else ""
            item["name"] = Path(doc["source_path"]).name if doc else ""
            item["total_chunks"] = doc["chunk_count"] if doc else 0
        self.json_response({"items": items})

    def _handle_rag_open(self) -> None:
        """B3：溯源跳转——打开已摄取文档的本地文件（仅限登记路径）。"""
        import rag
        qs = parse_qs(urlparse(self.path).query)
        path = qs.get("path", [""])[0]
        fp = rag._safe_relative(path)
        if not fp or str(fp) not in rag.registered_paths() or not fp.is_file():
            self.json_response({"error": "路径未登记或不存在"}, 400)
            return
        import os
        import webbrowser
        try:
            if os.name == "nt":
                os.startfile(str(fp))  # type: ignore[attr-defined]
            else:
                webbrowser.open(fp.as_uri())
        except OSError as exc:
            self.json_response({"error": f"打开文件失败: {exc}"}, 500)
            return
        self.json_response({"ok": True})

    def _serve_media(self, path: str) -> None:
        """B1：提供 /media/* 静态图片（限制在 MEDIA_DIR 内）。"""
        name = path[len("/media/"):].replace("\\", "/")
        if "/" in name or ".." in name:
            self.json_response({"error": "非法路径"}, 400)
            return
        fp = MEDIA_DIR / name
        if not fp.is_file():
            self.json_response({"error": "文件不存在"}, 404)
            return
        ext = fp.suffix.lower()
        ctype = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "application/octet-stream")
        body = fp.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _handle_extract_tags(self, data: dict[str, Any]) -> None:
        """B5：AI 自动打标签（草稿，R3 不落库）。返回建议 + 置信度，前端确认后写入。"""
        title = str(data.get("title", "")).strip()
        content = str(data.get("content", "")).strip()
        if not title or not content:
            self.json_response({"error": "标题和题目内容不能为空"}, 400)
            return
        result = extract_tags(
            title,
            content,
            str(data.get("course", "")).strip(),
            str(data.get("topic", "")).strip(),
        )
        self.json_response(result)

    def _handle_generate_variants(self, problem_id: int) -> None:
        """A4：生成 3 道变式（AI 或离线模板），仅返回草稿不落库（R3）。"""
        problem = row("SELECT * FROM problems WHERE id = ?", (problem_id,))
        if not problem:
            self.json_response({"error": "题目不存在"}, 404)
            return
        variants_source, variants = generate_variants(problem)
        self.json_response({"variants": variants, "source": variants_source})

    def _handle_save_variants(self, problem_id: int, data: dict[str, Any]) -> None:
        """A4：用户确认后保存变式到题根（R3 确认落库）。"""
        variants = data.get("variants")
        if not isinstance(variants, list) or not variants:
            self.json_response({"error": "变式列表不能为空"}, 400)
            return
        clean = []
        for v in variants:
            if not isinstance(v, dict):
                continue
            mode = str(v.get("mode", "")).strip()
            title = str(v.get("title", "")).strip()
            content = str(v.get("content", "")).strip()
            answer = str(v.get("answer", "")).strip()
            if title and content:
                clean.append({"mode": mode, "title": title, "content": content, "answer": answer})
        if not clean:
            self.json_response({"error": "变式内容不合法"}, 400)
            return
        existing = row("SELECT variants FROM problems WHERE id = ?", (problem_id,))
        if not existing:
            self.json_response({"error": "题目不存在"}, 404)
            return
        try:
            old = json.loads(existing["variants"]) if existing["variants"] else []
        except json.JSONDecodeError:
            old = []
        merged = old + clean
        with DB_LOCK, db() as conn:
            conn.execute("UPDATE problems SET variants = ?, updated_at = ? WHERE id = ?",
                         (json.dumps(merged, ensure_ascii=False), now(), problem_id))
        self.json_response({"ok": True, "count": len(clean), "total": len(merged)})

    def _handle_import(self, data: dict[str, Any]) -> None:
        """导入：先自动备份当前数据库，再参数化写入，避免注入与数据丢失。"""
        problems = data.get("problems")
        if not isinstance(problems, list):
            self.json_response({"error": "导入数据格式错误（缺少 problems 列表）"}, 400)
            return
        # 版本兼容性校验（防未来 schema 不一致的备份被误加载）
        data_version = int(data.get("version", 1))
        if data_version > 1:
            self.json_response({"error": f"备份来自更新的版本 (v{data_version})，请升级应用后再导入"}, 400)
            return
        # 自动备份
        backup = DB_PATH.with_name(DB_PATH.stem + f".bak.{now().replace(':', '')}.db")
        try:
            shutil.copy(DB_PATH, backup)
        except OSError as exc:
            self.json_response({"error": f"备份失败: {exc}"}, 500)
            return
        try:
            with DB_LOCK, db() as conn:
                conn.execute("DELETE FROM hints")
                conn.execute("DELETE FROM reviews")
                conn.execute("DELETE FROM problems")
                for p in problems:
                    if not isinstance(p, dict):
                        continue
                    pid = int(p.get("id", 0))
                    title = str(p.get("title", "")).strip()
                    content = str(p.get("content", "")).strip()
                    if not title or not content:
                        continue
                    conn.execute("""
                        INSERT INTO problems(id, title, course, topic, content, my_attempt, error_type,
                                             error_path, trap_note, shortcut, fix_action,
                                             tags, tags_status,
                                             mastery, ease_factor, repetition, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 2.5, 0, ?, ?)
                    """, (
                        pid, title, str(p.get("course", "")).strip(), str(p.get("topic", "")).strip(),
                        content, str(p.get("my_attempt", "")).strip(), normalize_error_type(p.get("error_type", "待诊断")),
                        str(p.get("error_path", "")).strip(), str(p.get("trap_note", "")).strip(),
                        str(p.get("shortcut", "")).strip(), str(p.get("fix_action", "")).strip(),
                        json.dumps(p.get("tags", []), ensure_ascii=False), str(p.get("tags_status", "none")).strip(),
                        clamp_mastery(int(p.get("mastery", 1))),
                        str(p.get("created_at", now())), str(p.get("updated_at", now())),
                    ))
                # 导入提示记录
                for h in data.get("hints", []):
                    if not isinstance(h, dict):
                        continue
                    conn.execute(
                        "INSERT INTO hints(problem_id, level, content, created_at) VALUES (?, ?, ?, ?)",
                        (int(h.get("problem_id", 0)), int(h.get("level", 1)),
                         str(h.get("content", "")).strip(), str(h.get("created_at", now()))),
                    )
                # 导入复习记录
                for rv in data.get("reviews", []):
                    if not isinstance(rv, dict):
                        continue
                    conn.execute(
                        "INSERT INTO reviews(problem_id, due_date, interval_days, result, completed, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (int(rv.get("problem_id", 0)), str(rv.get("due_date", "")).strip(),
                         int(rv.get("interval_days", 1)), str(rv.get("result", "")).strip(),
                         int(rv.get("completed", 0)), str(rv.get("created_at", now()))),
                    )
        except (ValueError, KeyError) as exc:
            self.json_response({"error": f"导入失败: {exc}"}, 400)
            return
        invalidate_settings_cache()
        self.json_response({"ok": True, "imported": len(problems), "backup": str(backup)})

    def _handle_batch(self, data: dict[str, Any]) -> None:
        """批量操作：删除/标记掌握度/切换星标。"""
        ids = data.get("ids")
        action = str(data.get("action", "")).strip()
        if not isinstance(ids, list) or not ids or not action:
            self.json_response({"error": "参数不合法 (ids/action)"}, 400)
            return
        with DB_LOCK, db() as conn:
            for pid in ids:
                pid = int(pid)
                if action == "delete":
                    conn.execute("DELETE FROM problems WHERE id = ?", (pid,))
                elif action == "mastery" and isinstance(value, int):
                    conn.execute("UPDATE problems SET mastery = ?, updated_at = ? WHERE id = ?",
                                 (clamp_mastery(value), now(), pid))
                elif action == "star":
                    conn.execute("UPDATE problems SET starred = CASE WHEN starred THEN 0 ELSE 1 END, updated_at = ? WHERE id = ?",
                                 (now(), pid))
        self.json_response({"ok": True, "affected": len(ids)})

    # ── PUT ──────────────────────────────────────────────

    def do_PUT(self) -> None:
        if not self._csrf_ok():
            self.json_response({"error": "请求来源不被信任 (缺少 X-Requested-With)"}, 403)
            return
        path = urlparse(self.path).path
        try:
            data = self.read_json()
            if path == "/api/settings":
                self._handle_update_settings(data)
                return
            if path == "/api/profile":
                from profile import update as _profile_update
                _profile_update(data)
                self.json_response({"ok": True})
                return
            match = re.fullmatch(r"/api/problems/(\d+)", path)
            if match:
                self._handle_update_problem(int(match.group(1)), data)
                return
            match = re.fullmatch(r"/api/reviews/(\d+)/reschedule", path)
            if match:
                self._handle_reschedule_review(int(match.group(1)))
                return
            self.json_response({"error": "接口不存在"}, 404)
        except Exception as exc:
            self._safe_error(exc)

    def _handle_update_settings(self, data: dict[str, Any]) -> None:
        allowed = {"api_base", "model", "temperature", "fast_model", "heavy_model", "vision_model"}
        values = []
        for key in allowed:
            if key in data:
                values.append((key, str(data[key]).strip()))
        with DB_LOCK, db() as conn:
            conn.executemany("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", values)
        # 密钥不落库（R4）：仅存于内存（会话级）或 keys.enc（可选加密文件）
        key = str(data.get("api_key", "")).strip()
        if key and key != "••••••••":
            set_runtime_key(key)
        master_password = str(data.get("master_password", "")).strip()
        if master_password:
            set_master_password(master_password)
            from keystore import save_key
            if key and key != "••••••••":
                save_key(key, master_password)
        invalidate_settings_cache()
        self.json_response({"ok": True, "has_api_key": bool(display_settings().get("has_api_key"))})

    def _handle_update_problem(self, problem_id: int, data: dict[str, Any]) -> None:
        existing = row("SELECT * FROM problems WHERE id = ?", (problem_id,))
        if not existing:
            self.json_response({"error": "题目不存在"}, 404)
            return
        fields = ["title", "course", "topic", "content", "my_attempt", "error_type",
                  "error_path", "trap_note", "shortcut", "fix_action", "mastery", "starred"]
        merged = {field: data.get(field, existing[field]) for field in fields}
        merged["mastery"] = clamp_mastery(int(merged["mastery"]))
        merged["title"] = str(merged["title"]).strip()
        merged["content"] = str(merged["content"]).strip()
        merged["error_type"] = normalize_error_type(merged["error_type"])
        if not merged["title"] or not merged["content"]:
            self.json_response({"error": "标题和题目内容不能为空"}, 400)
            return
        # B5（R3）：tags 显式提交才算「草稿确认」，落库并置 confirmed；否则保留原状
        tags = existing["tags"]
        tags_status = existing["tags_status"]
        if data.get("tags") is not None:
            tags = json.dumps(data["tags"], ensure_ascii=False)
            tags_status = "confirmed"
        with DB_LOCK, db() as conn:
            conn.execute("""
                UPDATE problems SET title=?, course=?, topic=?, content=?, my_attempt=?, error_type=?,
                                    error_path=?, trap_note=?, shortcut=?, fix_action=?,
                                    mastery=?, starred=?, tags=?, tags_status=?, updated_at=?
                WHERE id=?
            """, tuple(merged[field] for field in fields) + (tags, tags_status, now(), problem_id))
        self.json_response({"ok": True})

    def _handle_reschedule_review(self, review_id: int) -> None:
        """手动控制：把复习提前到今天。"""
        review = row("SELECT * FROM reviews WHERE id = ?", (review_id,))
        if not review:
            self.json_response({"error": "复习任务不存在"}, 404)
            return
        with DB_LOCK, db() as conn:
            conn.execute("UPDATE reviews SET due_date = ? WHERE id = ?", (date.today().isoformat(), review_id))
        self.json_response({"ok": True})

    # ── DELETE ───────────────────────────────────────────

    def do_DELETE(self) -> None:
        if not self._csrf_ok():
            self.json_response({"error": "请求来源不被信任 (缺少 X-Requested-With)"}, 403)
            return
        path = urlparse(self.path).path
        try:
            match = re.fullmatch(r"/api/problems/(\d+)", path)
            if match:
                with DB_LOCK, db() as conn:
                    cursor = conn.execute("DELETE FROM problems WHERE id = ?", (int(match.group(1)),))
                    if cursor.rowcount == 0:
                        self.json_response({"error": "题目不存在"}, 404)
                        return
                graph.update_progress()
                self.json_response({"ok": True})
                return
            match = re.fullmatch(r"/api/graph/concepts/(\d+)", path)
            if match:
                if not graph.delete_concept(int(match.group(1))):
                    self.json_response({"error": "概念不存在或仍有子概念/绑定题目"}, 400)
                    return
                self.json_response({"ok": True})
                return
            match = re.fullmatch(r"/api/rag/doc/(\d+)", path)
            if match:
                import rag
                if not rag.delete_doc(int(match.group(1))):
                    self.json_response({"error": "文档不存在"}, 404)
                    return
                self.json_response({"ok": True})
                return
            match = re.fullmatch(r"/api/exam/papers/(\d+)", path)
            if match:
                import exam
                if not exam.delete_paper(int(match.group(1))):
                    self.json_response({"error": "试卷不存在"}, 404)
                    return
                self.json_response({"ok": True})
                return
            self.json_response({"error": "接口不存在"}, 404)
        except Exception as exc:
            self._safe_error(exc)
