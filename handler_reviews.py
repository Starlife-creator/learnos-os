"""ReviewsMixin — Handler 复习队列/完成/调度域。

业务逻辑（队列优先级与交错、今日小结、完成复习的事务写回、顺延）已下沉
`review_session.py`（ADR 0010）；这里只做 HTTP 适配：解析请求 → 调领域函数 → 写响应。
"""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import review_session


class ReviewsMixin:
    def _handle_list_reviews(self) -> None:
        """复习队列。优先级排序（逾期久 > 错因权重 > 记忆脆弱 > 掌握度低 > 带漏点）
        + 交错 + 每日上限。?mode=plain 关闭交错（A7 遗留参数保留）。"""
        qs = parse_qs(urlparse(self.path).query)
        self.json_response(review_session.queue(
            self.subject, plain=qs.get("mode", [""])[0] == "plain"))

    def _handle_today_summary(self) -> None:
        """今日复盘摘要（A7）：题目口径（闪卡不在此页，见 ADR 0013）。"""
        self.json_response(review_session.today_summary(self.subject))

    def _handle_complete_review(self, review_id: int, data: dict[str, Any]) -> None:
        try:
            payload = review_session.complete(review_id, data.get("rating", 2), self.subject)
        except review_session.ReviewError as exc:
            self.json_response({"error": exc.message}, exc.status)
            return
        self.json_response(payload)

    def _handle_reschedule_review(self, review_id: int) -> None:
        """手动控制：把复习提前到今天。"""
        try:
            review_session.reschedule(review_id)
        except review_session.ReviewError as exc:
            self.json_response({"error": exc.message}, exc.status)
            return
        self.json_response({"ok": True})
