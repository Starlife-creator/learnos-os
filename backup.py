"""一键备份/还原（JSON 全量导出/导入，工作区内迁移）。

- 导出：全部业务表（不含密钥——settings.api_key 本就不落库），JSON 下载。
- 还原：先在工作区内把当前库改名为 .bak 时间戳备份，再重建空库并回填。
"""
from __future__ import annotations

import json
import shutil
import time
from datetime import date
from pathlib import Path
from typing import Any

from db import DB_LOCK, db, rows, now
from config import APP_DIR, LOG

BACKUP_TABLES = [
    "concepts",
    "rag_docs",
    "exam_papers",
    "problems",
    "learner_profile",
    "settings",
    "hints",
    "reviews",
    "oral_sessions",
    "concept_links",
    "concept_progress",
    "rag_chunks",
    "exam_questions",
]


def export_backup() -> dict[str, Any]:
    data: dict[str, list[dict[str, Any]]] = {}
    with db() as conn:
        for t in BACKUP_TABLES:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (t,)
            ).fetchone()
            if exists:
                data[t] = [dict(r) for r in conn.execute(f'SELECT * FROM "{t}"').fetchall()]
    return {"version": 1, "exported_at": now(), "tables": data}


def _table_sql(conn: Any, name: str) -> str:
    r = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone()
    return r[0] if r and r[0] else ""


def auto_backup_if_due() -> Path | None:
    """C7 定时自动备份：每天首次调用时复制现库到 backups/auto_YYYY-MM-DD.db，保留最近 7 份。

    幂等：当日已有备份则跳过（以文件存在为准，无额外状态）。
    """
    import shutil
    from db import DB_PATH as _db_path
    backups_dir = APP_DIR / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    pattern = f"auto_{today}_*.db"
    if any(backups_dir.glob(pattern)):
        return None
    if not _db_path.is_file():
        return None
    stamp = time.strftime("%H%M%S")
    target = backups_dir / f"auto_{today}_{stamp}.db"
    shutil.copy2(_db_path, target)
    keep = sorted(backups_dir.glob("auto_*.db"))
    for old in keep[:-7]:
        try:
            old.unlink()
        except OSError:
            pass
    LOG.info("自动备份已创建: %s（保留最近 %d 份）", target.name, 7)
    return target


def restore_backup(raw: str) -> dict[str, Any]:
    """还原：备份现库 → 重建 → 回填。返回统计。"""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise ValueError("备份文件不是合法 JSON")
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("备份文件版本不兼容")
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        raise ValueError("备份内容缺失 tables")

    # 1) 现库改名为 .bak 时间戳（工作区内）
    from db import DB_PATH as _db_path
    if _db_path.is_file():
        bak = _db_path.with_name(
            f"{_db_path.stem}_restore_{int(time.time())}.bak")
        shutil.copy2(_db_path, bak)
        LOG.info("还原前已备份现库: %s", bak.name)

    # 2) 重建空库（SCHEMA + 全部迁移 + 索引）
    from db import init_db
    if _db_path.is_file():
        _db_path.unlink()
    for ext in (".db-wal", ".db-shm"):
        p = Path(str(_db_path) + ext)
        if p.is_file():
            p.unlink()
    init_db()

    # 3) 按 FK 顺序回填
    counts: dict[str, int] = {}
    with DB_LOCK, db() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        for t in BACKUP_TABLES:
            rows_in = tables.get(t, [])
            if not rows_in:
                continue
            sql = _table_sql(conn, t)
            if not sql:
                continue
            cols = [c for c in rows_in[0].keys() if c in {r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')}]
            if not cols:
                continue
            conn.execute("DELETE FROM " + t)
            placeholders = ",".join("?" for _ in cols)
            col_sql = ",".join(f'"{c}"' for c in cols)
            conn.executemany(
                f'INSERT INTO "{t}" ({col_sql}) VALUES ({placeholders})',
                [tuple(r.get(c) for c in cols) for r in rows_in],
            )
            counts[t] = len(rows_in)
        conn.execute("PRAGMA foreign_keys = ON")
    LOG.info("还原完成: %s", counts)
    return {"restored": counts}
