"""一键备份/还原（JSON 全量导出/导入，工作区内迁移）。

- 导出：全部业务表（不含密钥——settings.api_key 本就不落库），JSON 下载。
- 还原：先在工作区内把当前库改名为 .bak 时间戳备份，再重建空库并回填。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from datetime import date
from pathlib import Path
from typing import Any

from db import DB_LOCK, db, rows, now, close_all_connections
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
    # v18+ 增量业务表（体检 P0-2 补齐；缺任一张 → 还原后对应功能数据清零）
    "subjects",           # v18 学科注册表（自建学科依赖）
    "study_checkins",     # v21 学习小组打卡
    "bank_scores",        # v22 题库 AI 评分历史
    "bank_attempts",      # 题库作答记录
    "bank_problems",      # 多题型改造的题库错题建档
    "mastery_log",        # 掌握度日志（报告数据源）
    "gamification",       # 成就 / 积分
    "cards",              # v25 概念闪卡（主动回忆）
    "card_reviews",       # v25 闪卡评分日志
]


def _trash_dir() -> Path:
    """还原/裁剪的可逆回收站（位于已 gitignore 的 backups/ 内）。"""
    d = APP_DIR / "backups" / "trash"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _move_to_trash(path: Path) -> None:
    """可逆裁剪：rename 到 trash 目录（非 unlink），避免直接删除文件。

    用 rename 而非删除：① 崩溃安全；② 对会拦截 unlink 的沙箱环境友好；
    ③ 需要时可从 trash 找回。
    """
    target = _trash_dir() / path.name
    if target.exists():
        target = _trash_dir() / f"{path.stem}_{int(time.time())}{path.suffix}"
    os.replace(path, target)


def _prune_trash(max_age_days: int = 30) -> None:
    """trash 最大龄清理：仅生产环境真正删除（unlink 在沙箱被拦截时静默跳过）。"""
    d = APP_DIR / "backups" / "trash"
    if not d.is_dir():
        return
    cutoff = time.time() - max_age_days * 86400
    for f in d.glob("*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass



def export_backup() -> dict[str, Any]:
    data: dict[str, list[dict[str, Any]]] = {}
    with db() as conn:
        for t in BACKUP_TABLES:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (t,)
            ).fetchone()
            if exists:
                data[t] = [dict(r) for r in conn.execute(f'SELECT * FROM "{t}"').fetchall()]
    # 完整性校验（P4a）：对"导出表数据"做规范化序列化后计算 sha256。
    # 规范化约定（sort_keys + separators）必须与 restore_backup 的校验侧完全一致，
    # 否则二次序列化不一致会误报。零密钥依赖，覆盖"损坏/篡改"主要失败模式。
    tables_bytes = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "version": 1,
        "exported_at": now(),
        "tables": data,
        "sha256": hashlib.sha256(tables_bytes).hexdigest(),
    }


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
    close_all_connections()  # R4：连接复用后必须落盘 WAL + 释放文件锁，才能安全拷库
    stamp = time.strftime("%H%M%S")
    target = backups_dir / f"auto_{today}_{stamp}.db"
    shutil.copy2(_db_path, target)
    keep = sorted(backups_dir.glob("auto_*.db"))
    for old in keep[:-7]:
        try:
            _move_to_trash(old)  # 可逆裁剪：移入 trash（非 unlink）
        except OSError:
            try:
                old.unlink()
            except OSError:
                pass
    _prune_trash()
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

    # 完整性校验（P4a）：仅当携带 sha256 时校验；旧备份缺字段则降级为警告，不阻断还原。
    # 校验侧与导出侧共用同一规范化序列化（sort_keys + separators），避免二次序列化误报。
    expect = payload.get("sha256")
    if expect:
        computed = hashlib.sha256(
            json.dumps(tables, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if computed != expect:
            raise ValueError("备份完整性校验失败（sha256 不匹配，可能损坏或被篡改）")
    elif tables is not None:
        LOG.warning("备份缺少 sha256 字段，跳过完整性校验（兼容旧格式）")

    # 1) 现库整体 rename 为 .bak 时间戳（同目录，非 unlink：原子、崩溃安全、对沙箱友好）
    from db import DB_PATH as _db_path
    # R4：还原是整库级操作，必须关闭**所有线程**的连接（本 handler 线程 + 并发 worker），
    # 否则 Windows 上 rename 被任一打开的连接锁住会失败；同时 checkpoint 落盘 WAL。
    close_all_connections()
    if _db_path.is_file():
        bak = _db_path.with_name(
            f"{_db_path.stem}_restore_{int(time.time())}.bak")
        os.replace(_db_path, bak)  # 旧库 rename 为 .bak，腾出真实路径给新库
        # WAL/SHM 一并 rename 到 .bak 旁，避免 unlink
        for ext in (".db-wal", ".db-shm"):
            p = Path(str(_db_path) + ext)
            if p.is_file():
                try:
                    os.replace(p, Path(str(bak) + ext))
                except OSError:
                    pass
        LOG.info("还原前已备份现库: %s", bak.name)

    # 2) 重建空库（SCHEMA + 全部迁移 + 索引）：connect 会新建文件，无需 unlink
    from db import init_db
    init_db()
    _prune_trash()

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
