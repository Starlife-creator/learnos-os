"""CardsMixin — 概念闪卡 HTTP 领域（Handler 拆分，模式与其它 Mixin 一致）。"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse
from typing import Any

import cards
from config import LOG


class CardsMixin:
    def _handle_list_cards(self) -> None:
        """GET /api/cards：列卡片（可选 ?status=）＋概览统计。"""
        qs = parse_qs(urlparse(self.path).query)
        status = qs.get("status", [""])[0]
        items = cards.list_cards(self.subject, status)
        self.json_response({"items": items, "stats": cards.stats(self.subject)})

    def _handle_list_due_cards(self) -> None:
        """GET /api/cards/due：到期复习队列（翻卡用）。"""
        items = cards.due_cards(self.subject)
        self.json_response({"items": items, "total": len(items)})

    def _handle_create_card(self, data: dict[str, Any]) -> None:
        """POST /api/cards：新建卡，或确认某张 AI/离线草稿（传其 id 即覆盖）。"""
        try:
            cid = cards.create_card(
                card_id=_int_or(data.get("id"), None),
                subject=self.subject,
                concept_id=_int_or(data.get("concept_id"), 0),
                cue=str(data.get("cue", "") or ""),
                answer=str(data.get("answer", "") or ""),
                kind=str(data.get("kind", "qa") or "qa"),
                source=str(data.get("source", "manual") or "manual"),
                status="active",
            )
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 400)
            return
        self.json_response({"id": cid})

    def _handle_generate_card_drafts(self, data: dict[str, Any]) -> None:
        """POST /api/cards/generate：为概念生成闪卡草稿（AI / 离线降级，不落库）。"""
        concept_id = _int_or(data.get("concept_id"), 0)
        if not concept_id or not cards.concept_lookup(concept_id):
            self.json_response({"error": "请先选择有效的概念"}, 400)
            return
        # 仅当配置了 AI 且本次请求要求 AI 时才校验 heavy 配额（离线草稿不消耗外部 API）
        use_ai = bool(data.get("use_ai", True))
        if use_ai:
            try:
                from ai import ai_configured
                if ai_configured() and not self._ai_quota("heavy"):
                    return
            except Exception as exc:  # pragma: no cover - 防御性
                LOG.debug("AI 可用性探测失败，按离线处理: %s", exc)
                use_ai = False
        try:
            drafts = cards.generate_drafts(self.subject, concept_id, use_ai=use_ai)
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 400)
            return
        self.json_response({"drafts": drafts, "concept_id": concept_id})

    def _handle_review_card(self, card_id: int, data: dict[str, Any]) -> None:
        """POST /api/cards/(卡片id)/review：评分（1-4）并调度下次。"""
        try:
            result = cards.review_card(card_id, int(data.get("rating", 2)))
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 404 if "不存在" in str(exc) else 400)
            return
        self.json_response(result)

    def _handle_learning_path(self) -> None:
        """GET /api/learn/path：按先修链给出主动学习路径（现在该学/替补什么）。"""
        qs = parse_qs(urlparse(self.path).query)
        try:
            threshold = float(qs.get("threshold", ["0.6"])[0])
        except ValueError:
            threshold = 0.6
        import graph
        self.json_response(graph.learning_path(self.subject, threshold))

    def _handle_delete_card(self, card_id: int) -> None:
        """POST /api/cards/(卡片id)/delete：删除卡片（评分日志级联）。"""
        ok = cards.delete_card(card_id)
        self.json_response({"ok": ok})


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default