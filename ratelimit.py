"""进程内失败限流（P4b，Tier C）。

仅对「导出令牌校验失败」做内存计数，挡住针对导出/还原端点的爆破，
不引入令牌过期/轮换（推迟项见 ADR-P4b-expire）。

设计约束（对齐优化方案「收益最大化、风险最小化」）：
- 零第三方依赖、纯标准库；状态进程内，重启自然清零（本地运维可接受）。
- 只统计「失败」：正确令牌即时清零该客户端计数，不影响正常常驻令牌使用。
- 回环地址（127.0.0.1 / ::1 / 127.x）阈值放宽，避免本机测试与偶发重试误伤。
- 时间窗滑动计数，窗口外的失败自动过期，无需后台清理线程。
- 阈值在调用时读取环境变量，测试可在运行时覆盖而无需重载模块。
"""
from __future__ import annotations

import os
import time
from collections import defaultdict


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _is_loopback(ip: str) -> bool:
    return ip in ("127.0.0.1", "::1", "localhost") or ip.startswith("127.")


def _window() -> float:
    return _env_float("LEARNOS_RL_WINDOW", 60.0)


def _limit_for(ip: str) -> int:
    # 回环放宽：本机单用户重试/测试不应被误杀
    if _is_loopback(ip):
        return _env_int("LEARNOS_RL_MAX_LOOPBACK", 100)
    return _env_int("LEARNOS_RL_MAX_REMOTE", 10)


def _prune(ip: str, now: float) -> list[float]:
    window = _window()
    recent = [t for t in _failures[ip] if now - t < window]
    if len(recent) != len(_failures[ip]):
        _failures[ip] = recent
    return recent


# key = 客户端 IP；value = 失败时间戳列表
_failures: dict[str, list[float]] = defaultdict(list)


def register_failure(ip: str) -> bool:
    """记录一次失败校验；返回 True 表示已超过阈值、应返 429。"""
    now = time.time()
    recent = _prune(ip, now)
    recent.append(now)
    _failures[ip] = recent
    return len(recent) > _limit_for(ip)


def is_blocked(ip: str) -> bool:
    """该客户端当前是否处于限流状态（不发失败也可见）。"""
    return len(_prune(ip, time.time())) > _limit_for(ip)


def clear(ip: str) -> None:
    """正确令牌后清零该客户端失败计数。"""
    _failures.pop(ip, None)


def clear_all() -> None:
    """供测试隔离。"""
    _failures.clear()
