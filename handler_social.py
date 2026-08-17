"""SocialMixin — 学习小组打卡与无答案进度分享（§34.2/§42.3）。

与 ProblemsMixin 同被 Handler 继承，故可复用 _export_token_ok 做导出鉴权。
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse, parse_qs

import social
from config import EXPORT_TOKEN


class SocialMixin:
    # ── 打卡 ──────────────────────────────────────────
    def _handle_social_checkin(self, data: dict[str, Any]) -> None:
        """POST：新增打卡。body: {subject?, minutes, note?, date?}。"""
        subj = self._subject_of(data)
        try:
            minutes = int(data.get("minutes", 0))
        except (TypeError, ValueError):
            minutes = 0
        note = str(data.get("note", "") or "")
        day = str(data.get("date", "") or "").strip() or None
        cid = social.add_checkin(subj, minutes, note, day)
        self.json_response({"ok": True, "id": cid, "streak": social.streak(subj)})

    def _handle_social_checkins(self) -> None:
        """GET：最近打卡列表。?subject=&limit=."""
        qs = parse_qs(urlparse(self.path).query)
        subj = qs.get("subject", [""])[0] or None
        try:
            limit = int(qs.get("limit", ["30"])[0])
        except (ValueError, IndexError):
            limit = 30
        self.json_response({
            "checkins": social.list_checkins(subj, limit),
            "streak": social.streak(subj),
            "total_minutes": social.total_minutes(subj),
        })

    def _handle_social_streak(self) -> None:
        """GET：连续打卡天数。?subject=."""
        qs = parse_qs(urlparse(self.path).query)
        subj = qs.get("subject", [""])[0] or None
        self.json_response({"streak": social.streak(subj), "total_minutes": social.total_minutes(subj)})

    def _handle_export_social(self) -> None:
        """GET：无答案进度分享包（本地优先）。需导出令牌（§16.6）。"""
        qs = parse_qs(urlparse(self.path).query)
        if not self._export_token_ok():
            self.json_response(
                {"error": "缺少有效的导出令牌（?token= 或 X-Export-Token）"}, 401
            )
            return
        subj = qs.get("subject", [""])[0] or None
        payload = social.export_social(subj)
        self.json_response(payload)
