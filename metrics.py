"""本地运维指标采集（`ai_telemetry` 聚合），无 HTTP 知识。

ADR 0010 第四批：`_handle_metrics` 里的 SQL 与统计口径下沉到这里，handler 侧只做
鉴权（`_metrics_authorized`）、合并缓存/开关状态、写响应。

口径说明：
- `latency_ms_p95` 是**近似**值：样本 < 20 时取最大值，否则取排序后的 95 分位。
  样本量太小谈分位数没有意义，最大值反而更能反映最坏体验。
- 采集失败一律降级为空指标，不抛出 —— 指标端点坏掉不能拖垮主流程。
"""
from __future__ import annotations

from typing import Any

from config import LOG
from db import DB_LOCK, db


def _p95(values: list[int]) -> int | None:
    if not values:
        return None
    if len(values) < 20:
        return max(values)
    return sorted(values)[int(len(values) * 0.95) - 1]


def collect() -> dict[str, Any]:
    """返回 {"tokens", "cached_tokens", "latency_ms_p95", "provider_last_error"}。"""
    tokens = 0
    cached_tokens = 0
    latency_p95: int | None = None
    provider_last_error: dict[str, str] | None = None
    try:
        with DB_LOCK, db() as conn:
            r = conn.execute(
                "SELECT COALESCE(SUM(tokens),0), COALESCE(SUM(cached_tokens),0) FROM ai_telemetry"
            ).fetchone()
            if r:
                tokens, cached_tokens = r[0], r[1]
            lat = [x[0] for x in conn.execute(
                "SELECT latency_ms FROM ai_telemetry WHERE latency_ms > 0 ORDER BY latency_ms"
            ).fetchall()]
            latency_p95 = _p95(lat)
            le = conn.execute(
                "SELECT error_kind, ts FROM ai_telemetry WHERE error_kind <> '' ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            if le:
                provider_last_error = {"kind": le[0], "ts": le[1]}
    except Exception as exc:  # 指标端点失败不应拖垮主流程
        LOG.debug("采集指标失败（忽略）: %s", exc)
    return {
        "tokens": tokens,
        "cached_tokens": cached_tokens,
        "latency_ms_p95": latency_p95,
        "provider_last_error": provider_last_error,
    }
