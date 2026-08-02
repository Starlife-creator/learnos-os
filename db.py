"""数据访问层：SQLite 连接管理与通用查询。"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import sqlite3

from config import DB_PATH, SCHEMA, DEFAULT_SETTINGS, LOG

DB_LOCK = threading.Lock()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """版本化数据库迁移：每次 schema 变更新增一个版本条目。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row and row[0] is not None else 0

    # v1: v0.1 → v0.2 — 添加 ease_factor 和 repetition 列
    if current < 1:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(problems)").fetchall()}
        if "ease_factor" not in cols:
            conn.execute("ALTER TABLE problems ADD COLUMN ease_factor REAL NOT NULL DEFAULT 2.5")
        if "repetition" not in cols:
            conn.execute("ALTER TABLE problems ADD COLUMN repetition INTEGER NOT NULL DEFAULT 0")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (1, ?)", (now(),))
        LOG.info("数据库已迁移到 v1 (添加 ease_factor/repetition)")

    # v2: 掌握度趋势日志表
    if current < 2:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mastery_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                avg_mastery REAL NOT NULL,
                count INTEGER NOT NULL
            )
        """)
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (2, ?)", (now(),))
        LOG.info("数据库已迁移到 v2 (添加 mastery_log)")


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DB_LOCK, db() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.executemany(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            DEFAULT_SETTINGS.items(),
        )
    LOG.info("数据库已初始化: %s", DB_PATH)


def rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with db() as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def row(query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    result = rows(query, params)
    return result[0] if result else None


def settings_dict(include_secret: bool = False) -> dict[str, str]:
    import os
    data = {item["key"]: item["value"] for item in rows("SELECT key, value FROM settings")}
    env_key = os.environ.get("PHYSICS_OS_API_KEY", "")
    if env_key:
        data["api_key"] = env_key
        data["key_source"] = "environment"
    else:
        data["key_source"] = "local"
    if not include_secret:
        key = data.get("api_key", "")
        data["api_key"] = "••••••••" if key else ""
        data["has_api_key"] = bool(key)
    return data
