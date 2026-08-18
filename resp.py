"""统一响应封装与集中错误码（P6：增量采用，不做一次性大改写）。

设计约定（与优化方案 Tier B 一致）：
- `api_ok` / `api_err` 供「本次新增代码路径」与逐步迁移的接口使用；
  旧接口保持原样，避免爆炸式 diff（见 Out-of-Scope）。
- 错误计数进入 `/api/metrics` 的 `errors` 节（与 P1b 合并同一端点，不另起炉灶）；
  进程内计数，重启即清零，仅作本地运维观测。
- 仅依赖 typing，零业务/循环依赖，任何模块可安全导入。
"""
from __future__ import annotations

from typing import Any

# 集中错误码 → (HTTP 状态, 中文文案)。新增错误码在此登记一处即可全站一致。
ERRORS: dict[str, tuple[int, str]] = {
    "FORBIDDEN": (401, "缺少或无效的导出令牌"),
    "BAD_REQUEST": (400, "请求格式不正确"),
    "NOT_FOUND": (404, "资源不存在"),
    "BAD_BACKUP": (400, "备份损坏或不兼容"),
    "CONFLICT": (409, "资源状态冲突"),
    "RATE_LIMITED": (429, "尝试次数过多，请稍后再试"),
    "SERVER_ERROR": (500, "服务器内部错误，请查看日志"),
}

_error_counts: dict[str, int] = {}


def api_ok(handler: Any, data: Any = None, status: int = 200) -> None:
    """成功响应：统一包裹为 {"ok": true, ...}。"""
    payload: dict[str, Any] = {"ok": True}
    if isinstance(data, dict):
        payload.update(data)
    elif data is not None:
        payload["data"] = data
    handler.json_response(payload, status)


def api_err(handler: Any, code: str, status: int | None = None, **extra: Any) -> None:
    """错误响应：{"error": 码, "message": 中文文案, ...}，并累计错误计数。

    ERRORS 元组约定为 (HTTP 状态, 中文文案)，故此处解包为 st, msg（顺序不可反）。
    """
    st, msg = ERRORS.get(code, (500, "未知错误"))
    _error_counts[code] = _error_counts.get(code, 0) + 1
    handler.json_response({"error": code, "message": msg, **extra}, status or st)


def error_counts() -> dict[str, int]:
    """供 /api/metrics 读取（返回副本，避免外部修改内部计数）。"""
    return dict(_error_counts)


def reset_error_counts() -> None:
    """供测试隔离使用。"""
    _error_counts.clear()
