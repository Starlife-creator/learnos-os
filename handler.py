"""HTTP 请求处理器：路由分发与 JSON 响应。"""
from __future__ import annotations

import json
import re
import shutil
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler
from typing import Any
from urllib.parse import urlparse, parse_qs

from config import STATIC_DIR, LOG, DB_PATH
from db import DB_LOCK, db, now, row, rows, settings_dict
from ai import call_ai, fallback_hint, problem_prompt, invalidate_settings_cache, set_runtime_key, display_settings
from review import compute_review, clamp_mastery
from oral import start_oral, continue_oral

# ── 写请求安全闸门（CSRF 轻量版）──
# 跨站页无法设置自定义请求头（会触发预检而本服务不响应 OPTIONS），
# 因此同源的 X-Requested-With 头即可作为写请求的合法来源证明。
X_HEADER = "X-Requested-With"
X_VALUE = "PhysicsStudyOS"

# 写幂等：客户端携带 X-Request-Id，重复提交返回首次结果，杜绝重复建题。
_IDEMPOTENCY: dict[str, tuple[int, dict[str, Any]]] = {}
_IDEMPOTENCY_TTL = 3600


def _prune_idempotency() -> None:
    if len(_IDEMPOTENCY) < 512:
        return
    cutoff = datetime.now().timestamp() - _IDEMPOTENCY_TTL
    stale = [k for k, (ts, _) in _IDEMPOTENCY.items() if ts < cutoff]
    for k in stale:
        _IDEMPOTENCY.pop(k, None)


class Handler(SimpleHTTPRequestHandler):
    server_version = "PhysicsStudyOS/0.3"

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
            if path == "/api/reviews":
                self.json_response(rows("""
                    SELECT r.*, p.title, p.course, p.topic, p.content, p.my_attempt
                    FROM reviews r JOIN problems p ON p.id = r.problem_id
                    WHERE r.completed = 0 ORDER BY r.due_date ASC
                """))
                return
            if path == "/api/settings":
                self.json_response(display_settings())
                return
            if path == "/api/trend":
                self._handle_trend()
                return
            match = re.fullmatch(r"/api/oral/(\d+)", path)
            if match:
                self._handle_get_oral(int(match.group(1)))
                return
            if path == "/api/export":
                self._handle_export()
                return
            if path == "/api/health":
                self.json_response({"ok": True, "version": "0.3.0"})
                return
            super().do_GET()
        except Exception as exc:
            self._safe_error(exc)

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

        items = rows(
            f"SELECT id, title, course, topic, error_type, mastery, starred, created_at, updated_at FROM problems{where} ORDER BY {order} LIMIT ? OFFSET ?",
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
        self.json_response({"stats": stats, "due": due["count"] if due else 0, "topics": topics, "recent": recent, "recent_activity": recent_activity, "course_stats": course_stats})

    def _handle_trend(self) -> None:
        log = rows("SELECT day, avg_mastery, count FROM mastery_log ORDER BY id DESC LIMIT 60")
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        summary = row("""
            SELECT COUNT(*) AS week_reviews,
                   COALESCE(ROUND(AVG(CASE WHEN CAST(result AS INTEGER) >= 3 THEN 1.0 ELSE 0 END) * 100, 0), 0) AS week_accuracy
            FROM reviews WHERE completed = 1 AND created_at >= ?
        """, (week_ago,)) or {}
        week_new = row("SELECT COUNT(*) AS count FROM problems WHERE created_at >= ?", (week_ago,))
        self.json_response({
            "points": list(reversed(log)),
            "summary": {
                "week_reviews": int(summary.get("week_reviews", 0)),
                "week_accuracy": int(summary.get("week_accuracy", 0)),
                "week_new": int(week_new["count"]) if week_new else 0,
            },
        })

    def _handle_get_oral(self, session_id: int) -> None:
        """返回一次口试会话的完整 transcript。"""
        item = row("SELECT * FROM oral_sessions WHERE id = ?", (session_id,))
        if not item:
            self.json_response({"error": "口试会话不存在"}, 404)
            return
        item["transcript"] = json.loads(item["transcript"]) if item["transcript"] else []
        self.json_response(item)

    def _handle_export(self) -> None:
        """只读导出：返回全部题目、提示与复习记录的 JSON。"""
        data = {
            "version": 1,
            "exported_at": now(),
            "problems": rows("SELECT id, title, course, topic, content, my_attempt, error_type, mastery, created_at, updated_at FROM problems ORDER BY id"),
            "hints": rows("SELECT problem_id, level, content, created_at FROM hints ORDER BY id"),
            "reviews": rows("SELECT problem_id, due_date, interval_days, result, completed, created_at FROM reviews ORDER BY id"),
        }
        self.json_response(data)

    def _handle_get_problem(self, problem_id: int) -> None:
        item = row("SELECT * FROM problems WHERE id = ?", (problem_id,))
        if not item:
            self.json_response({"error": "题目不存在"}, 404)
            return
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
            match = re.fullmatch(r"/api/reviews/(\d+)/complete", path)
            if match:
                self._handle_complete_review(int(match.group(1)), data)
                return
            match = re.fullmatch(r"/api/oral/(\d+)/end", path)
            if match:
                self._handle_oral_end(int(match.group(1)))
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
        with DB_LOCK, db() as conn:
            cursor = conn.execute("""
                INSERT INTO problems(title, course, topic, content, my_attempt, error_type, mastery, ease_factor, repetition, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 2.5, 0, ?, ?)
            """, (
                title, str(data.get("course", "")).strip(), str(data.get("topic", "")).strip(),
                content, str(data.get("my_attempt", "")).strip(), str(data.get("error_type", "待诊断")),
                clamp_mastery(int(data.get("mastery", 1))), stamp, stamp,
            ))
            problem_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO reviews(problem_id, due_date, interval_days, created_at) VALUES (?, ?, 1, ?)",
                (problem_id, (date.today() + timedelta(days=1)).isoformat(), stamp),
            )
        result = {"id": problem_id}
        if rid:
            _IDEMPOTENCY[rid] = (datetime.now().timestamp(), result)
            _prune_idempotency()
        self.json_response(result, 201)

    def _handle_hint(self, problem_id: int, data: dict[str, Any]) -> None:
        level = max(1, min(3, int(data.get("level", 1))))
        problem = row("SELECT * FROM problems WHERE id = ?", (problem_id,))
        if not problem:
            self.json_response({"error": "题目不存在"}, 404)
            return
        existing = row("SELECT content FROM hints WHERE problem_id = ? AND level = ?", (problem_id, level))
        if existing:
            self.json_response({"content": existing["content"], "source": "saved"})
            return
        source = "ai"
        try:
            hint = call_ai(problem_prompt(problem, level))
        except Exception as exc:
            hint = fallback_hint(problem, level)
            source = "fallback"
            LOG.warning("提示降级 (problem=%s, level=%d): %s", problem_id, level, exc)
        with DB_LOCK, db() as conn:
            conn.execute(
                "INSERT INTO hints(problem_id, level, content, created_at) VALUES (?, ?, ?, ?)",
                (problem_id, level, hint, now()),
            )
        self.json_response({"content": hint, "source": source})

    def _handle_complete_review(self, review_id: int, data: dict[str, Any]) -> None:
        review = row("SELECT * FROM reviews WHERE id = ?", (review_id,))
        if not review:
            self.json_response({"error": "复习任务不存在"}, 404)
            return
        rating = max(1, min(4, int(data.get("rating", 2))))
        problem = row("SELECT ease_factor, repetition FROM problems WHERE id = ?", (review["problem_id"],))
        prev_ease = problem["ease_factor"] if problem else 2.5
        prev_rep = problem["repetition"] if problem else 0

        result = compute_review(rating, review["interval_days"], prev_ease, prev_rep)
        next_due = (date.today() + timedelta(days=result.interval_days)).isoformat()
        with DB_LOCK, db() as conn:
            conn.execute("UPDATE reviews SET completed = 1, result = ? WHERE id = ?", (str(rating), review_id))
            conn.execute(
                "INSERT INTO reviews(problem_id, due_date, interval_days, created_at) VALUES (?, ?, ?, ?)",
                (review["problem_id"], next_due, result.interval_days, now()),
            )
            conn.execute(
                "UPDATE problems SET mastery = ?, ease_factor = ?, repetition = ?, updated_at = ? WHERE id = ?",
                (result.mastery, result.ease_factor, result.repetition, now(), review["problem_id"]),
            )
            self._log_mastery(conn)
        self.json_response({"next_due": next_due, "interval_days": result.interval_days})

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

    def _handle_oral_end(self, session_id: int) -> None:
        """结束口试会话，将状态设为 finished。"""
        session = row("SELECT * FROM oral_sessions WHERE id = ?", (session_id,))
        if not session:
            self.json_response({"error": "口试会话不存在"}, 404)
            return
        with DB_LOCK, db() as conn:
            conn.execute("UPDATE oral_sessions SET status = 'finished' WHERE id = ?", (session_id,))
        self.json_response({"ok": True})

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
                        INSERT INTO problems(id, title, course, topic, content, my_attempt, error_type, mastery, ease_factor, repetition, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 2.5, 0, ?, ?)
                    """, (
                        pid, title, str(p.get("course", "")).strip(), str(p.get("topic", "")).strip(),
                        content, str(p.get("my_attempt", "")).strip(), str(p.get("error_type", "待诊断")),
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
        value = data.get("value")
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
        allowed = {"api_base", "model", "temperature"}
        values = []
        for key in allowed:
            if key in data:
                values.append((key, str(data[key]).strip()))
        with DB_LOCK, db() as conn:
            conn.executemany("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", values)
        # 密钥不落库：仅存于内存（会话级），重启后需重新录入
        key = str(data.get("api_key", "")).strip()
        if key and key != "••••••••":
            set_runtime_key(key)
        invalidate_settings_cache()
        self.json_response({"ok": True, "has_api_key": bool(display_settings().get("api_key"))})

    def _handle_update_problem(self, problem_id: int, data: dict[str, Any]) -> None:
        existing = row("SELECT * FROM problems WHERE id = ?", (problem_id,))
        if not existing:
            self.json_response({"error": "题目不存在"}, 404)
            return
        fields = ["title", "course", "topic", "content", "my_attempt", "error_type", "mastery", "starred"]
        merged = {field: data.get(field, existing[field]) for field in fields}
        merged["mastery"] = clamp_mastery(int(merged["mastery"]))
        merged["title"] = str(merged["title"]).strip()
        merged["content"] = str(merged["content"]).strip()
        if not merged["title"] or not merged["content"]:
            self.json_response({"error": "标题和题目内容不能为空"}, 400)
            return
        with DB_LOCK, db() as conn:
            conn.execute("""
                UPDATE problems SET title=?, course=?, topic=?, content=?, my_attempt=?, error_type=?, mastery=?, starred=?, updated_at=?
                WHERE id=?
            """, tuple(merged[field] for field in fields) + (now(), problem_id))
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
            if not match:
                self.json_response({"error": "接口不存在"}, 404)
                return
            with DB_LOCK, db() as conn:
                cursor = conn.execute("DELETE FROM problems WHERE id = ?", (int(match.group(1)),))
                if cursor.rowcount == 0:
                    self.json_response({"error": "题目不存在"}, 404)
                    return
            self.json_response({"ok": True})
        except Exception as exc:
            self._safe_error(exc)
