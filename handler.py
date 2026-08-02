"""HTTP 请求处理器：路由分发与 JSON 响应。"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler
from typing import Any
from urllib.parse import urlparse

from config import STATIC_DIR, LOG
from db import DB_LOCK, db, now, row, rows, settings_dict
from ai import call_ai, fallback_hint, problem_prompt
from review import compute_review, clamp_mastery
from oral import start_oral, continue_oral


class Handler(SimpleHTTPRequestHandler):
    server_version = "PhysicsStudyOS/0.2"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

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
                self.json_response(rows("SELECT * FROM problems ORDER BY id DESC"))
                return
            match = re.fullmatch(r"/api/problems/(\d+)", path)
            if match:
                self._handle_get_problem(int(match.group(1)))
                return
            if path == "/api/reviews":
                self.json_response(rows("""
                    SELECT r.*, p.title, p.course, p.topic, p.content, p.my_attempt
                    FROM reviews r JOIN problems p ON p.id = r.problem_id
                    WHERE r.completed = 0 ORDER BY r.due_date ASC
                """))
                return
            if path == "/api/settings":
                self.json_response(settings_dict())
                return
            if path == "/api/health":
                self.json_response({"ok": True, "version": "0.2.0"})
                return
            super().do_GET()
        except Exception as exc:
            self._safe_error(exc)

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
        recent = rows("SELECT id, title, course, topic, error_type, mastery, created_at FROM problems ORDER BY id DESC LIMIT 5")
        self.json_response({"stats": stats, "due": due["count"] if due else 0, "topics": topics, "recent": recent})

    def _handle_get_problem(self, problem_id: int) -> None:
        item = row("SELECT * FROM problems WHERE id = ?", (problem_id,))
        if not item:
            self.json_response({"error": "题目不存在"}, 404)
            return
        item["hints"] = rows("SELECT level, content, created_at FROM hints WHERE problem_id = ? ORDER BY level", (problem_id,))
        self.json_response(item)

    # ── POST ─────────────────────────────────────────────

    def do_POST(self) -> None:
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
            if path == "/api/oral/start":
                self._handle_oral_start(data)
                return
            if path == "/api/oral/respond":
                self._handle_oral_respond(data)
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
        self.json_response({"id": problem_id}, 201)

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
            hint += f"\n\n（AI 未调用：{exc}）"
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
        self.json_response({"next_due": next_due, "interval_days": result.interval_days})

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

    # ── PUT ──────────────────────────────────────────────

    def do_PUT(self) -> None:
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
            self.json_response({"error": "接口不存在"}, 404)
        except Exception as exc:
            self._safe_error(exc)

    def _handle_update_settings(self, data: dict[str, Any]) -> None:
        allowed = {"api_base", "api_key", "model", "temperature"}
        values = []
        for key in allowed:
            if key not in data or (key == "api_key" and data[key] == "••••••••"):
                continue
            values.append((key, str(data[key]).strip()))
        with DB_LOCK, db() as conn:
            conn.executemany("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", values)
        self.json_response({"ok": True, "has_api_key": bool(settings_dict(True).get("api_key"))})

    def _handle_update_problem(self, problem_id: int, data: dict[str, Any]) -> None:
        existing = row("SELECT * FROM problems WHERE id = ?", (problem_id,))
        if not existing:
            self.json_response({"error": "题目不存在"}, 404)
            return
        fields = ["title", "course", "topic", "content", "my_attempt", "error_type", "mastery"]
        merged = {field: data.get(field, existing[field]) for field in fields}
        merged["mastery"] = clamp_mastery(int(merged["mastery"]))
        with DB_LOCK, db() as conn:
            conn.execute("""
                UPDATE problems SET title=?, course=?, topic=?, content=?, my_attempt=?, error_type=?, mastery=?, updated_at=?
                WHERE id=?
            """, tuple(merged[field] for field in fields) + (now(), problem_id))
        self.json_response({"ok": True})

    # ── DELETE ───────────────────────────────────────────

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        try:
            match = re.fullmatch(r"/api/problems/(\d+)", path)
            if not match:
                self.json_response({"error": "接口不存在"}, 404)
                return
            with DB_LOCK, db() as conn:
                conn.execute("DELETE FROM problems WHERE id = ?", (int(match.group(1)),))
            self.json_response({"ok": True})
        except Exception as exc:
            self._safe_error(exc)
