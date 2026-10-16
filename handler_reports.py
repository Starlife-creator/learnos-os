"""ReportsMixin — Handler 仪表盘/周期报告/趋势/分析域的 HTTP 适配层。

业务逻辑（SQL/聚合/降级）已下沉到 reports.py，这里只做「取 subject → 写响应」。
"""
from __future__ import annotations

import reports


class ReportsMixin:
    def _handle_dashboard(self) -> None:
        self.json_response(reports.dashboard(self.subject))

    def _handle_progress(self) -> None:
        """§42.1/§42.2 可见进步仪表盘 + 微学习节奏（≤10 分钟单元）。

        对抗 85% 三周流失：让「努力→可见进步→奖励→回返」的循环每会话可呈现。
        """
        self.json_response(reports.progress(self.subject))

    def _handle_weekly_report(self) -> None:
        """GET /api/report/weekly：周报详情（供前端详情弹窗）。"""
        self.json_response(reports.weekly_report(self.subject))

    def _handle_monthly_report(self) -> None:
        """GET /api/report/monthly：近 30 天周期报告详情。"""
        self.json_response(reports.monthly_report(self.subject))

    def _handle_trend(self) -> None:
        # 此前未传 subject，恒返回 physics → 切换学科后趋势数据不跟随。
        self.json_response(reports.trend_data(self.subject))

    def _handle_analytics(self) -> None:
        # 同 _handle_trend：此前忽略 ?subject=，多学科场景下恒显示 physics。
        self.json_response(reports.analytics_data(self.subject))

    def _handle_gamification(self) -> None:
        self.json_response(reports.game_state())
