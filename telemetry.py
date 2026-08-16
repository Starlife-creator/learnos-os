"""C6 AI 调用遥测：零依赖，只记元数据（route/model/延迟/token/成败），不含任何内容。"""
from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any

from config import LOG
from db import DB_LOCK, db, now

_RETENTION_DAYS = 90  # 滚动清理：保留 90 天（防数据膨胀）


def record(route: str = "", model: str = "", latency_ms: int = 0,
           tokens: int = 0, ok: bool = False, error_kind: str = "",
           start: float | None = None, cached: int = 0) -> None:
    """记一条 AI 调用记录；写入后顺手清理过期数据（成本极低）。

    cached：输入侧 prompt 缓存命中 token（DeepSeek prompt_cache_hit_tokens /
    OpenAI prompt_tokens_details.cached_tokens），0 = 未命中或不支持。
    """
    if start is not None:
        latency_ms = int((time.monotonic() - start) * 1000)
    try:
        with DB_LOCK, db() as conn:
            conn.execute(
                "INSERT INTO ai_telemetry(ts, route, model, latency_ms, tokens, ok, error_kind, cached_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (now(), route[:64], model[:64], int(latency_ms), int(tokens),
                 1 if ok else 0, error_kind[:32], int(cached)),
            )
            cutoff = (date.today() - timedelta(days=_RETENTION_DAYS)).isoformat()
            conn.execute("DELETE FROM ai_telemetry WHERE ts < ?", (cutoff,))
    except Exception as exc:
        LOG.debug("遥测写入失败（可忽略）: %s", exc)


def summary() -> dict[str, Any]:
    """近 7 天遥测摘要：调用数/失败率/平均延迟/token/缓存命中率/慢请求 TOP3 路由。"""
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    try:
        rows = _rows("""
            SELECT COUNT(*) AS calls, SUM(ok) AS ok_count, AVG(latency_ms) AS avg_latency,
                   SUM(tokens) AS tokens, SUM(cached_tokens) AS cached_tokens
            FROM ai_telemetry WHERE ts >= ?
        """, (week_ago,))
        slow = _rows("""
            SELECT route, MAX(latency_ms) AS worst FROM ai_telemetry
            WHERE ts >= ? GROUP BY route ORDER BY worst DESC LIMIT 3
        """, (week_ago,))
    except Exception as exc:
        LOG.debug("遥测摘要失败（可忽略）: %s", exc)
        return {}
    row = rows[0] if rows else {}
    calls = int(row.get("calls") or 0)
    ok_count = int(row.get("ok_count") or 0)
    tokens = int(row.get("tokens") or 0)
    cached_tokens = int(row.get("cached_tokens") or 0)
    return {
        "calls": calls,
        "failed": calls - ok_count,
        "fail_rate": round((calls - ok_count) / calls, 3) if calls else 0.0,
        "avg_latency_ms": int(round(row.get("avg_latency") or 0)),
        "tokens": tokens,
        "cached_tokens": cached_tokens,
        # 近似命中率：命中输入 token / 总 token（分母含输出，故为下界估计）
        "cache_hit_rate": round(cached_tokens / tokens, 3) if tokens else 0.0,
        "slow_routes": [s["route"] or "(未知)" for s in slow if s.get("route")],
    }


def _rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    from db import rows
    return rows(query, params)


__all__ = ["record", "summary"]
