"""全局配置与路径常量。"""
from __future__ import annotations

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ── 日志器（必须先于其它使用，避免 NameError）──
LOG = logging.getLogger("learnos")

BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
STATIC_DIR = BUNDLE_ROOT / "static"
DB_PATH = Path(os.environ.get("LEARNOS_DB", APP_DIR / "learnos.db"))

# ── 网络配置 ──
HOST = os.environ.get("LEARNOS_HOST", "127.0.0.1")
try:
    PORT = int(os.environ.get("LEARNOS_PORT", "8765"))
except ValueError:
    LOG.warning("LEARNOS_PORT 值无效，使用默认端口 8765")
    PORT = 8765

# ── AI 配置（支持环境变量，优先级高于本地数据库）──
API_KEY_ENV = os.environ.get("LEARNOS_API_KEY", "").strip()
API_BASE_ENV = os.environ.get("LEARNOS_API_BASE", "").strip()
MODEL_ENV = os.environ.get("LEARNOS_MODEL", "").strip()

LOG_FILE = APP_DIR / "learnos.log"
MEDIA_DIR = APP_DIR / "media"


class SecretRedactor(logging.Filter):
    """脱敏过滤器：遮蔽日志中的 API Key 等敏感片段。"""

    _PATTERNS = [
        re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
        re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]{8,}"),
        re.compile(r"((?:api_)?key['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9_\-]{8,}"),
        re.compile(r"(token['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9_\-]{8,}"),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.getMessage())
        for pat in self._PATTERNS:
            msg = pat.sub(lambda m: m.group(1) + "***REDACTED***" if m.groups() else "***REDACTED***", msg)
        record.msg = msg
        record.args = ()
        return True


def setup_logging() -> None:
    """配置控制台 + 滚动文件日志，并统一脱敏。幂等。"""
    if LOG.handlers:
        return
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    # Windows 控制台默认 GBK，强制 UTF-8 避免中文日志乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    redactor = SecretRedactor()

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.addFilter(redactor)
    LOG.addHandler(ch)

    try:
        fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(redactor)
        LOG.addHandler(fh)
    except OSError:
        # 文件不可写时不影响控制台日志
        pass


setup_logging()

SCHEMA = """
CREATE TABLE IF NOT EXISTS problems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    course TEXT NOT NULL DEFAULT '',
    topic TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    my_attempt TEXT NOT NULL DEFAULT '',
    error_type TEXT NOT NULL DEFAULT '待诊断',
    mastery INTEGER NOT NULL DEFAULT 1,
    ease_factor REAL NOT NULL DEFAULT 2.5,
    repetition INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    starred INTEGER NOT NULL DEFAULT 0,
    error_path TEXT NOT NULL DEFAULT '',
    trap_note TEXT NOT NULL DEFAULT '',
    shortcut TEXT NOT NULL DEFAULT '',
    fix_action TEXT NOT NULL DEFAULT '',
    state INTEGER NOT NULL DEFAULT 0,
    stability REAL NOT NULL DEFAULT 0.0,
    difficulty REAL NOT NULL DEFAULT 0.0,
    tags TEXT NOT NULL DEFAULT '[]',
    tags_suggested TEXT NOT NULL DEFAULT '[]',
    tags_status TEXT NOT NULL DEFAULT '',
    variants TEXT NOT NULL DEFAULT '[]',
    concept_ids TEXT NOT NULL DEFAULT '[]',
    media_path TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS hints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id INTEGER NOT NULL,
    level INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(problem_id) REFERENCES problems(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id INTEGER NOT NULL,
    due_date TEXT NOT NULL,
    interval_days INTEGER NOT NULL DEFAULT 1,
    completed INTEGER NOT NULL DEFAULT 0,
    result TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(problem_id) REFERENCES problems(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS oral_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    transcript TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""

# D1（R4 合规）：密钥一律不落库。仅保存非敏感配置。
DEFAULT_SETTINGS = {
    "api_base": "https://api.openai.com/v1",
    "model": "",
    "temperature": "0.3",
    "fast_model": "",
    "heavy_model": "",
    "vision_model": "",
}
