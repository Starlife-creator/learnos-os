"""R1/R2/R5 鉴权原语：导出一次性挑战 + 暴露态判定 + 审计。

设计要点：
- 导出/还原令牌不再经 /api/bootstrap 返回（P1-2 根因：静态同源 secret 被无鉴权回显）。
- 改为「同源 CSRF 端点签发一次性、短 TTL HMAC 挑战」：客户端每次导出/还原前向
  POST /api/export/challenge 取一个 60s 内单次有效的挑战令牌，用后即焚（防重放）。
- R5 升级：挑战签名绑定客户端 IP（HMAC over nonce|exp|ip），换 IP 重放即拒；
  密钥取 EXPORT_TOKEN（可用 LEARNOS_EXPORT_TOKEN 环境变量固定，跨重启稳定）。
- EXPORT_TOKEN 仅作服务端 HMAC 签名密钥，永不随响应外发。
- is_exposed() 决定服务器是否对网络开放；暴露态下的写操作由 handler 层要求 Bearer 令牌（R2）。
- audit() 追加破坏性操作最小审计行（data/audit.log，不记敏感内容）。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from pathlib import Path

from config import EXPORT_TOKEN, HOST, API_TOKEN, APP_DIR

# 一次性导出挑战（R1/R5）：内存态，进程重启即清空（本地运维可接受）。
_CHALLENGES: dict[str, float] = {}  # token -> 过期时间戳
_CHALLENGE_TTL = float(os.environ.get("LEARNOS_CHALLENGE_TTL", "60"))
_CHALLENGE_LOCK = threading.Lock()

# 审计日志（R5）：破坏性操作最小留痕，追加写、无缓冲（写失败不阻塞请求）。
_AUDIT_PATH = APP_DIR / "data" / "audit.log"
_AUDIT_LOCK = threading.Lock()


def is_exposed() -> bool:
    """服务器是否对网络开放（非回环地址）。由 HOST 决定，与 ALLOW_LAN 无关。"""
    return HOST not in ("127.0.0.1", "::1", "localhost")


def bearer_ok(provided: str | None) -> bool:
    """R2：暴露态写鉴权——校验 `Authorization: Bearer <LEARNOS_API_TOKEN>`。

    回环模式不调用此函数（由 handler 层短路）；暴露模式无 token 时一律 False，
    配合 app.py 启动守卫（无 token 拒绝启动）形成双保险。
    """
    if not API_TOKEN or not provided:
        return False
    expected = "Bearer " + API_TOKEN
    return hmac.compare_digest(provided, expected)


def _sign(nonce: str, exp: int, ip: str) -> str:
    """HMAC-SHA256(EXPORT_TOKEN, nonce|exp|ip)。密钥不在响应中出现。"""
    return hmac.new(
        EXPORT_TOKEN.encode("utf-8"),
        f"{nonce}|{exp}|{ip}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def issue_export_challenge(ip: str = "") -> tuple[str, float]:
    """签发一次性、短 TTL、绑定客户端 IP 的 HMAC 挑战令牌。返回 (token, ttl_seconds)。"""
    # 签发前顺带回收过期未消费的挑战（此前该函数从未被调用 → 过期项永久驻留内存）
    _prune_challenges()
    nonce = secrets.token_hex(16)
    exp = int(time.time()) + int(_CHALLENGE_TTL)
    sig = _sign(nonce, exp, ip)
    token = f"{nonce}.{exp}.{sig}"
    with _CHALLENGE_LOCK:
        _CHALLENGES[token] = exp
    return token, _CHALLENGE_TTL


def verify_export_challenge(token: str | None, ip: str = "") -> bool:
    """校验一次性挑战：签名有效、IP 绑定、未过期、且未使用（用后删除，防重放）。

    ip 为空（测试桩/旧调用方）时跳过 IP 绑定校验；生产路径始终传真实对端 IP。
    """
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    nonce, exp_s, sig = parts
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if time.time() > exp:
        return False
    if not hmac.compare_digest(sig, _sign(nonce, exp, ip)):
        return False
    with _CHALLENGE_LOCK:
        # 取出即视为已消费（单次有效）
        if _CHALLENGES.pop(token, None) is None:
            return False
    return True


def audit(action: str, ok: bool = True, ip: str = "", detail: str = "") -> None:
    """R5：破坏性操作审计。追加 `ts|ip|action|ok|detail` 到 data/audit.log。

    写失败仅降级日志，不阻塞请求主路径（audit 是留痕不是关卡）。
    """
    try:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')}|{ip}|{action}|{'ok' if ok else 'fail'}|{detail}\n"
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_LOCK:
            with open(_AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass


def _prune_challenges() -> None:
    """清理过期挑战，避免内存无限增长（仅防御性，验证时已 pop 过期项）。"""
    now = time.time()
    with _CHALLENGE_LOCK:
        expired = [t for t, exp in _CHALLENGES.items() if exp <= now]
        for t in expired:
            _CHALLENGES.pop(t, None)
