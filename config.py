"""全局配置与路径常量。"""
from __future__ import annotations

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ── 日志器（必须先于其它使用，避免 NameError）──
LOG = logging.getLogger("physics_os")

BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
STATIC_DIR = BUNDLE_ROOT / "static"
DB_PATH = Path(os.environ.get("PHYSICS_OS_DB", APP_DIR / "physics_study.db"))

# ── 网络配置 ──
HOST = os.environ.get("PHYSICS_OS_HOST", "127.0.0.1")
try:
    PORT = int(os.environ.get("PHYSICS_OS_PORT", "8765"))
except ValueError:
    LOG.warning("PHYSICS_OS_PORT 值无效，使用默认端口 8765")
    PORT = 8765

# ── AI 配置（支持环境变量，优先级高于本地数据库）──
API_KEY_ENV = os.environ.get("PHYSICS_OS_API_KEY", "").strip()
API_BASE_ENV = os.environ.get("PHYSICS_OS_API_BASE", "").strip()
MODEL_ENV = os.environ.get("PHYSICS_OS_MODEL", "").strip()

LOG_FILE = APP_DIR / "physics_study.log"


class SecretRedactor(logging.Filter):
    """脱敏过滤器：遮蔽日志中的 API Key 等敏感片段。"""

    _PATTERNS = [
        re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
        re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]{8,}"),
        re.compile(r"(api_key['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9_\-]{8,}"),
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
    updated_at TEXT NOT NULL
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

DEFAULT_SETTINGS = {
    "api_base": "https://api.openai.com/v1",
    "api_key": "",
    "model": "",
    "temperature": "0.3",
}
