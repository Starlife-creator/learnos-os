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

    def _handle_generate_card_batch(self, data: dict[str, Any]) -> None:
        """POST /api/cards/generate-batch：C1 按概念里程碑清单批量出卡草稿（不落库）。

        body: {concept_ids: [概念id...], use_ai: bool}。逐概念独立出卡（单概念
        失败跳过），返回 {results: [{concept_id, concept_name, drafts}], failed}。
        """
        ids = data.get("concept_ids")
        if not isinstance(ids, list) or not ids:
            self.json_response({"error": "请提供概念 id 列表（concept_ids）"}, 400)
            return
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
            out = cards.generate_batch_drafts(self.subject, ids, use_ai=use_ai)
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 400)
            return
        if not out["results"]:
            self.json_response({"error": "全部概念均未产出草稿", "failed": out["failed"]}, 400)
            return
        self.json_response(out)

    def _handle_review_card(self, card_id: int, data: dict[str, Any]) -> None:
        """POST /api/cards/(卡片id)/review：评分（1-4）并调度下次。"""
        try:
            result = cards.review_card(card_id, int(data.get("rating", 2)))
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 404 if "不存在" in str(exc) else 400)
            return
        self.json_response(result)

    def _handle_undo_card_review(self, card_id: int) -> None:
        """POST /api/cards/(卡片id)/undo：撤销最近一次评分（D2，原子恢复 prev 快照）。"""
        try:
            result = cards.undo_review(card_id)
        except ValueError as exc:
            self.json_response({"error": str(exc)},
                               404 if "没有可撤销" in str(exc) else 400)
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

    def _handle_next_step(self) -> None:
        """GET /api/learn/next-step：U1 统一下一步（只读）。

        由状态计算（到期错题→到期闪卡→薄弱口试→下一未掌握→完成），
        任意入口（卡片/答题/口试/地图）调用得到同一条全局 next；
        与仪表盘内嵌的 next_step 字段走同一 graph.next_step，不因入口分叉。
        """
        import graph
        self.json_response(graph.next_step(self.subject))

    def _handle_delete_card(self, card_id: int) -> None:
        """POST /api/cards/(卡片id)/delete：删除卡片（评分日志级联）。"""
        ok = cards.delete_card(card_id)
        self.json_response({"ok": ok})


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default