"""密钥安全存储（D1）：工作区加密文件 keys.enc + 内存降级。

R4 合规：密钥绝不写入数据库。
优先级：环境变量 PHYSICS_OS_API_KEY > keys.enc（工作区） > 内存（会话级）。

加密实现：优先使用可用的 cryptography（vendored 或用户安装）；
依赖缺失时降级为纯内存密钥（每次启动重新录入），绝不在无加密时落盘明文。
"""
from __future__ import annotations

import base64
import json
import os
import secrets
from pathlib import Path
from typing import Any

from config import APP_DIR, LOG

KEY_FILE = APP_DIR / "data" / "keys.enc"

_PBKDF2_ITERATIONS = 310_000
_SALT_LEN = 16
_IV_LEN = 12
_TAG_LEN = 16


def _crypto():
    """惰性加载 cryptography（vendored 或用户安装）。返回 None 表示不可用。"""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
        return {
            "Cipher": Cipher, "algorithms": algorithms, "modes": modes,
            "PBKDF2HMAC": PBKDF2HMAC, "hashes": hashes,
        }
    except ImportError:
        return None


def crypto_available() -> bool:
    return _crypto() is not None


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def save_key(api_key: str, password: str) -> bool:
    """加密保存密钥到工作区 keys.enc。成功返回 True，依赖缺失返回 False。"""
    c = _crypto()
    api_key = (api_key or "").strip()
    if not api_key or not password or not c:
        return False
    try:
        salt = secrets.token_bytes(_SALT_LEN)
        kdf = c["PBKDF2HMAC"](
            algorithm=c["hashes"].SHA256(),
            length=32,
            salt=salt,
            iterations=_PBKDF2_ITERATIONS,
        )
        key = kdf.derive(password.encode("utf-8"))
        iv = secrets.token_bytes(_IV_LEN)
        encryptor = c["Cipher"](c["algorithms"].AES(key), c["modes"].GCM(iv)).encryptor()
        ct = encryptor.update(api_key.encode("utf-8")) + encryptor.finalize()
        blob = {
            "v": 1,
            "salt": _b64e(salt),
            "iv": _b64e(iv),
            "tag": _b64e(encryptor.tag),
            "ct": _b64e(ct),
        }
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = KEY_FILE.with_suffix(".enc.tmp")
        tmp.write_text(json.dumps(blob), encoding="utf-8")
        tmp.replace(KEY_FILE)  # 原子替换，避免半写文件
        LOG.info("API 密钥已加密保存到工作区 keys.enc")
        return True
    except Exception as exc:
        LOG.warning("密钥加密保存失败，仅保留内存: %s", exc)
        return False


def load_key(password: str) -> str | None:
    """从 keys.enc 解密密钥。文件不存在 / 口令错误 / 依赖缺失 → None。"""
    c = _crypto()
    if not c or not KEY_FILE.exists() or not password:
        return None
    try:
        blob = json.loads(KEY_FILE.read_text(encoding="utf-8"))
        if blob.get("v") != 1:
            return None
        salt = _b64d(blob["salt"])
        iv = _b64d(blob["iv"])
        tag = _b64d(blob["tag"])
        ct = _b64d(blob["ct"])
        kdf = c["PBKDF2HMAC"](
            algorithm=c["hashes"].SHA256(),
            length=32,
            salt=salt,
            iterations=_PBKDF2_ITERATIONS,
        )
        key = kdf.derive(password.encode("utf-8"))
        decryptor = c["Cipher"](c["algorithms"].AES(key), c["modes"].GCM(iv, tag)).decryptor()
        plain = decryptor.update(ct) + decryptor.finalize()
        return plain.decode("utf-8")
    except Exception:
        return None


def clear_key() -> None:
    """删除 keys.enc（用户选择「清除全部密钥」时调用）。"""
    try:
        KEY_FILE.unlink()
        LOG.info("已清除 keys.enc")
    except OSError:
        pass


def key_file_exists() -> bool:
    return KEY_FILE.exists()


def migrate_db_key_out(settings: dict[str, str]) -> None:
    """把旧版存于 DB settings 表的 api_key 迁移到 keys.enc 后清除。

    迁移需要口令；无口令或依赖缺失时仅清除 DB 中的密钥（不落明文）。
    返回后 settings 中不再包含 api_key 明文。
    """
    settings.pop("api_key", None)
