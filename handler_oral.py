"""OralMixin — 口试/费曼 Handler 方法。自 handler.py 原样迁移。"""
from __future__ import annotations

import json
from typing import Any

from config import LOG
from db import DB_LOCK, db, now, row
from oral import (
    start_oral, continue_oral, draft_oral_card, start_feynman,
    feynman_self_review, save_feynman_self_review,
)


class OralMixin:
    def _handle_get_oral(self, session_id: int) -> None:
        """返回一次口试会话的完整 transcript。"""
        item = row("SELECT * FROM oral_sessions WHERE id = ?", (session_id,))
        if not item:
            self.json_response({"error": "口试会话不存在"}, 404)
            return
        item["transcript"] = json.loads(item["transcript"]) if item["transcript"] else []
        self.json_response(item)

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
