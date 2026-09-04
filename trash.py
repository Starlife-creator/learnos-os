"""D3 回收站（v33）：删除前快照入 trash 表，保留期内可原样恢复。

设计要点：
- 快照与真实 DELETE 同事务：调用方在删除语句前调用 snapshot()，删除失败回滚时快照一并撤销；
- payload_json 按表分组存全行 dict（主表先于子表，恢复按序 INSERT 保证 FK 顺序）；
- 过期清理独立开关：settings.trash_retention_days（默认 3 日；0 = 永不自动清理），
  每次快照顺手清理过期项（trash 表极小，代价可忽略）；
- 恢复按 (kind, entity_id) 取最近一条未恢复记录；恢复成功标记 restored_at（不删行，审计可查）；
- 表均带 AUTOINCREMENT 主键：恢复用原 id 不与新行冲突；唯一键冲突（如 rag_docs.source_path
  被重新注册）时整事务回滚并报错，不留半恢复状态。
"""
from __future__ import annotations

import base64
import binascii
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from db import DB_LOCK, db, now, rows, settings_dict

# 支持的回收站实体类型（与各 delete 收口点一一对应）
KINDS = {"card", "concept", "material", "annotation", "rag_doc"}

# BLOB 列（如 rag_embeddings.vec）的 JSON 安全编码标记
_BLOB_TAG = "__b64__"


def _enc_row(r: dict[str, Any]) -> dict[str, Any]:
    """行 dict → JSON 安全 dict（bytes 列编为 base64 标记对象）。"""
    return {k: ({_BLOB_TAG: base64.b64encode(v).decode("ascii")} if isinstance(v, bytes) else v)
            for k, v in r.items()}


def _dec_value(v: Any) -> Any:
    """JSON 值 → 落库值（还原 base64 标记的 bytes）。"""
    if isinstance(v, dict) and set(v) == {_BLOB_TAG} and isinstance(v[_BLOB_TAG], str):
        try:
            return base64.b64decode(v[_BLOB_TAG])
        except (ValueError, binascii.Error):
            return v
    return v


def retention_days() -> int:
    """保留期（日）。0 = 永不自动清理（独立开关）。"""
    try:
        return int(str(settings_dict().get("trash_retention_days", "3") or 3))
    except (TypeError, ValueError):
        return 3


def purge_expired(conn: Any) -> int:
    """物理清理过期快照（调用方事务内执行）。返回清理行数。"""
    days = retention_days()
    if days <= 0:
        return 0
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    cur = conn.execute("DELETE FROM trash WHERE trashed_at < ? AND restored_at = ''", (cutoff,))
    return cur.rowcount


def snapshot(conn: Any, kind: str, entity_id: int,
             tables: list[tuple[str, str, tuple]]) -> None:
    """删除前快照：把主行 + 级联子行全量存入 trash（与真实 DELETE 同事务）。

    tables: [(表名, SELECT 语句, 参数)] — 表名即恢复时的目标表；
    顺序必须主表在前（恢复按序 INSERT，保证 FK 父行先落）。
    顺手清理过期项（trash 表极小）。
    """
    payload: dict[str, list[dict[str, Any]]] = {}
    for table, sql, params in tables:
        payload[table] = [_enc_row(dict(r)) for r in conn.execute(sql, params).fetchall()]
    conn.execute(
        "INSERT INTO trash(kind, entity_id, payload_json, trashed_at) VALUES (?, ?, ?, ?)",
        (kind, int(entity_id), json.dumps(payload, ensure_ascii=False), now()),
    )
    purge_expired(conn)


def _do_restore(conn: Any, t: Any) -> None:
    """按 payload 顺序逐表逐行 INSERT（原 id，含 BLOB 还原）。冲突抛 IntegrityError → 整事务回滚。"""
    payload: dict[str, list[dict[str, Any]]] = json.loads(t["payload_json"])
    for table, rws in payload.items():
        for r in rws:
            cols = list(r.keys())
            conn.execute(
                f"INSERT INTO {table}({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [_dec_value(r[c]) for c in cols],
            )
    conn.execute("UPDATE trash SET restored_at = ? WHERE id = ?", (now(), t["id"]))


def restore(kind: str, entity_id: int) -> dict[str, Any] | None:
    """按 (kind, entity_id) 恢复最近一条未恢复快照。无快照返回 None；冲突抛 sqlite3.IntegrityError。"""
    with DB_LOCK, db() as conn:
        t = conn.execute(
            "SELECT * FROM trash WHERE kind = ? AND entity_id = ? AND restored_at = '' "
            "ORDER BY id DESC LIMIT 1", (kind, int(entity_id))).fetchone()
        if t is None:
            return None
        _do_restore(conn, t)
        return dict(t)


def restore_by_id(trash_id: int) -> dict[str, Any] | None:
    """按 trash 行 id 恢复（回收站列表用）。已恢复/不存在返回 None。"""
    with DB_LOCK, db() as conn:
        t = conn.execute("SELECT * FROM trash WHERE id = ?", (int(trash_id),)).fetchone()
        if t is None or t["restored_at"]:
            return None
        _do_restore(conn, t)
        return dict(t)


def list_trash(limit: int = 200) -> list[dict[str, Any]]:
    """回收站列表（新→旧）。payload 过大不下发，只给恢复所需的元信息。"""
    out = []
    for r in rows(
        "SELECT id, kind, entity_id, trashed_at, restored_at "
        "FROM trash ORDER BY id DESC LIMIT ?", (int(limit),),
    ):
        d = dict(r)
        d["restorable"] = not d["restored_at"]
        out.append(d)
    return out


def startup_purge() -> None:
    """启动清理（init_db 调用，幂等）。"""
    try:
        with DB_LOCK, db() as conn:
            purge_expired(conn)
    except sqlite3.Error:
        pass  # 清理失败不阻塞启动
