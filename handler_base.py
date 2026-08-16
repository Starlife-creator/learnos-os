"""handler 拆分共享层：CSRF 头、幂等键、列表/交错助手。

首次拆分（material/oral）后遗留的模块级助手集中于此；
handler.py 与各领域 mixin 统一从这里导入，避免跨模块 NameError。
"""
from __future__ import annotations

import re
from typing import Any

X_HEADER = "X-Requested-With"
X_VALUE = "LearnOS"

# 写请求幂等键（R3）：同键短窗口内重复请求返回缓存结果，防网络重试产生重复数据
_IDEMPOTENCY: dict[str, tuple[int, dict[str, Any]]] = {}
_IDEMPOTENCY_TTL = 3600


def _as_str_list(value: Any) -> list[str]:
    """A8：收口 methods 输入——只接受字符串数组；单字符串视为一法；非法则忽略。"""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [v.strip() for v in value if isinstance(v, str) and v.strip()]
    return []


def _interleave(items: list[dict[str, Any]], key: str = "topic") -> list[dict[str, Any]]:
    """A7 交错练习：按 key 分桶后贪心轮转取卡，避免同知识点连续出现。

    每步选取剩余数量最多的桶且不等于上一个桶；若只剩一个桶则按原序补完。
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        k = str(item.get(key) or "未分类")
        buckets.setdefault(k, []).append(item)
    out: list[dict[str, Any]] = []
    last_key: str | None = None
    while buckets:
        candidates = [k for k in buckets if k != last_key]
        pool = candidates or list(buckets.keys())
        k = max(pool, key=lambda x: len(buckets[x]))
        out.append(buckets[k].pop(0))
        if not buckets[k]:
            del buckets[k]
        last_key = k
    return out


def _prune_idempotency() -> None:
    from datetime import datetime as _dt
    if len(_IDEMPOTENCY) < 512:
        return
    cutoff = _dt.now().timestamp() - _IDEMPOTENCY_TTL
    stale = [k for k, (ts, _) in _IDEMPOTENCY.items() if ts < cutoff]
    for k in stale:
        _IDEMPOTENCY.pop(k, None)
