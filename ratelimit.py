"""进程内限流（P4b Tier C + R3 AI 调用配额）。

两类语义、同一模块：
- 失败限流（P4b）：导出令牌校验失败计数，挡住爆破。
- AI 调用配额（R3）：按 IP + 档位（heavy/fast）滑动窗口计数，护外部 API 额度。

设计约束（对齐优化方案「收益最大化、风险最小化」）：
- 零第三方依赖、纯标准库；状态进程内，重启自然清零（本地运维可接受）。
- 只统计「失败」：正确令牌即时清零该客户端计数，不影响正常常驻令牌使用。
- 回环地址（127.0.0.1 / ::1 / 127.x）阈值放宽，避免本机测试与偶发重试误伤。
- 时间窗滑动计数，窗口外的失败自动过期，无需后台清理线程。
- 阈值在调用时读取环境变量，测试可在运行时覆盖而无需重载模块。
- R3 配额 fail-open：任何异常一律放行，绝不因限流器坏而阻塞正常学习。
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict

# C1b：两把独立锁——失败限流（鉴权热路径）与 AI 配额互不串行；
# 均在对应状态读写处加锁，避免滑动窗口 append+重赋值并发丢计数。
_fail_lock = threading.Lock()
_ai_lock = threading.Lock()


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
    # 用 .get 而非 [ip]：defaultdict 的取值访问会凭空创建空 key，造成无界增长
    existing = _failures.get(ip)
    if not existing:
        return []
    recent = [t for t in existing if now - t < window]
    if len(recent) != len(existing):
        if recent:
            _failures[ip] = recent
        else:
            _failures.pop(ip, None)  # 空 key 回收
    return recent


# key = 客户端 IP；value = 失败时间戳列表
_failures: dict[str, list[float]] = defaultdict(list)


def register_failure(ip: str) -> bool:
    """记录一次失败校验；返回 True 表示已超过阈值、应返 429。

    C1b：加锁防并发丢计数；fail-open——限流器自身异常不阻断鉴权（与 ai_quota_ok 一致）。
    """
    try:
        now = time.time()
        with _fail_lock:
            recent = _prune(ip, now)
            recent.append(now)
            _failures[ip] = recent
        return len(recent) > _limit_for(ip)
    except Exception:
        return False


def is_blocked(ip: str) -> bool:
    """该客户端当前是否处于限流状态（不发失败也可见）。"""
    with _fail_lock:
        return len(_prune(ip, time.time())) > _limit_for(ip)


def clear(ip: str) -> None:
    """正确令牌后清零该客户端失败计数。"""
    with _fail_lock:
        _failures.pop(ip, None)


def clear_all() -> None:
    """供测试隔离。"""
    with _fail_lock:
        _failures.clear()
    with _ai_lock:
        _ai_calls.clear()


# ── R3：AI 调用配额（滑动窗口，按 IP + 档位）──
# key = (ip, tier)；value = 调用时间戳列表。独立于失败限流，成功调用也计数。
_ai_calls: dict[tuple[str, str], list[float]] = defaultdict(list)


def _ai_limit_for(tier: str) -> int:
    # heavy（视觉/口试/变式/资料分析等重接口）更紧；fast（打标签等轻接口）略宽
    env_key = "LEARNOS_AI_MAX_HEAVY" if tier == "heavy" else "LEARNOS_AI_MAX_FAST"
    default = 10 if tier == "heavy" else 40
    return _env_int(env_key, default)


def ai_quota_ok(ip: str, tier: str = "fast") -> bool:
    """R3：AI 调用配额检查——窗口内调用数 < 阈值即放行并记账。

    fail-open：任何异常（环境变量损坏等）一律放行，绝不阻塞正常学习。
    调用方在 AI 入口处 `if not ai_quota_ok(ip, tier): 返 429`。
    """
    try:
        now = time.time()
        window = _window()
        key = (ip, tier)
        with _ai_lock:
            recent = _prune_calls(key, now, window)
            if len(recent) >= _ai_limit_for(tier):
                return False
            recent.append(now)
            _ai_calls[key] = recent
        return True
    except Exception:
        return True


def _prune_calls(key: tuple[str, str], now: float, window: float) -> list[float]:
    # 用 .get 而非 [key]：defaultdict 的取值访问会凭空创建空 key，造成无界增长
    existing = _ai_calls.get(key)
    if not existing:
        return []
    recent = [t for t in existing if now - t < window]
    if len(recent) != len(existing):
        if recent:
            _ai_calls[key] = recent
        else:
            _ai_calls.pop(key, None)  # 空 key 回收
    return recent
