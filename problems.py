"""错题领域层：题目 CRUD、分页检索、查重、批量操作与导入。

纯业务逻辑（SQL + 校验 + 事务），不含任何 HTTP 知识；handler_problems.py
只做薄适配（解析 query/body → 调这里 → 写响应）。领域层用 ProblemsError
表达 4xx 语义（`status` 默认 400），由 handler 转成响应体。
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import date, timedelta
from typing import Any

from config import APP_DIR
from db import DB_LOCK, db, now, row, rows
import db as db_module  # 模块引用：`db` 已被函数遮蔽，需用模块访问实时 DB_PATH
from domain_errors import DomainError
from errors import normalize_error_type
from mastery import set_problem_mastery
from review import clamp_mastery
import graph


class ProblemsError(DomainError):
    """错题领域层 4xx 语义：handler 据此输出 {"error": message} + status。"""


# 与 handler_base._as_str_list 同实现：领域层不得反向依赖 handler*（见
# test_fitness_functions.TestDomainLayerPurity），故这里各持一份等价实现。
def _as_str_list(value: Any) -> list[str]:
    """A8：收口 methods 输入——只接受字符串数组；单字符串视为一法；非法则忽略。"""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [v.strip() for v in value if isinstance(v, str) and v.strip()]
    return []


def list_problems(subject: str, *, q: str = "", sort: str = "time", page: int = 1,
                  limit: int = 50, prereq: str = "") -> dict[str, Any]:
    """错题列表：分页 + 关键词搜索 + 排序 + A2 先修链过滤。

    分页参数在这里兜底钳位（limit 1..200、page >= 1），调用方传脏值也不会放大查询；
    sort 白名单 {"time": id DESC, "mastery": mastery ASC}，其他值回落 time。
    """
    limit = min(max(int(limit), 1), 200)
    page = max(int(page), 1)
    offset = (page - 1) * limit

    if q:
        like = f"%{q}%"
        where = " WHERE subject = ? AND (title LIKE ? OR topic LIKE ? OR course LIKE ?)"
        params: tuple[Any, ...] = (subject, like, like, like)
    else:
        where, params = " WHERE subject = ?", (subject,)

    # A2 先修模式：?prereq=<concept_id> 过滤出该概念先修链上的历史错题
    if prereq.isdigit():
        chain = graph.prereq_chain(int(prereq))
        if chain:
            cond = " OR ".join("concept_ids LIKE ?" for _ in chain)
            chain_params = tuple(f"%,{cid},%" for cid in chain)
            if where:
                where += f" AND ({cond})"
            else:
                where = f" WHERE {cond}"
            params = params + chain_params

    order = {"time": "id DESC", "mastery": "mastery ASC"}.get(sort, "id DESC")
    items = rows(
        f"SELECT id, title, course, topic, content, my_attempt, error_path, fix_action, error_type, mastery, starred, tags, tags_status, created_at, updated_at FROM problems{where} ORDER BY {order} LIMIT ? OFFSET ?",
        params + (limit, offset),
    )
    total_row = row(f"SELECT COUNT(*) AS count FROM problems{where}", params)
    total = total_row["count"] if total_row else 0
    # 一次窗口函数查询拉回所有题的最近 3 次评分（替代每道题单独查）
    if items:
        ids = [item["id"] for item in items]
        placeholders = ",".join("?" for _ in ids)
        mini_rows = rows(f"""
            SELECT problem_id, result FROM (
                SELECT problem_id, result, id,
                    ROW_NUMBER() OVER (PARTITION BY problem_id ORDER BY id DESC) AS rn
                FROM reviews WHERE completed = 1 AND problem_id IN ({placeholders})
            ) WHERE rn <= 3 ORDER BY problem_id, id ASC
        """, tuple(ids))
        by_pid: dict[int, list[str]] = {pid: [] for pid in ids}
        for mr in mini_rows:
            by_pid[mr["problem_id"]].append(mr["result"])
        for item in items:
            item["recent_results"] = by_pid.get(item["id"], [])
    return {
        "items": items, "total": total, "page": page, "limit": limit,
        "pages": (total + limit - 1) // limit,
    }


def get_problem(problem_id: int) -> dict[str, Any]:
    """题目详情：JSON 字段容错解析 + 概念/先修告警 + 费曼自评 + 附件与提示。"""
    item = row("SELECT * FROM problems WHERE id = ?", (problem_id,))
    if not item:
        raise ProblemsError("题目不存在", 404)
    try:
        item["tags"] = json.loads(item["tags"]) if item["tags"] else []
    except (json.JSONDecodeError, TypeError):
        item["tags"] = []
    try:
        item["variants"] = json.loads(item["variants"]) if item["variants"] else []
    except (json.JSONDecodeError, TypeError):
        item["variants"] = []
    # A2：concept_ids 解析 + 先修掌握度告警
    item["concept_ids"] = graph.concept_ids_to_list(item.get("concept_ids") or "")
    item["prereq_warnings"] = graph.prereq_warnings(problem_id)
    # A8：一题多解
    try:
        item["methods"] = json.loads(item["methods"]) if item.get("methods") else []
    except (json.JSONDecodeError, TypeError):
        item["methods"] = []
    # A5：Feynman 自评表（已保存的最新一条）
    feynman = row(
        "SELECT self_review FROM oral_sessions "
        "WHERE problem_id = ? AND mode = 'feynman' AND self_review != '' "
        "ORDER BY id DESC LIMIT 1",
        (problem_id,),
    )
    if feynman:
        try:
            item["feynman_self_review"] = json.loads(feynman["self_review"])
        except json.JSONDecodeError:
            item["feynman_self_review"] = None
    else:
        item["feynman_self_review"] = None
    # B1：图片附件列表
    item["media_list"] = [p for p in (item.get("media_path") or "").split(",") if p.strip()]
    item["hints"] = rows("SELECT level, content, created_at FROM hints WHERE problem_id = ? ORDER BY level", (problem_id,))
    return item


def history(problem_id: int) -> list[dict[str, Any]]:
    """一道题的全部已完成复习记录（SM-2 轨迹）。"""
    return rows("""
        SELECT due_date, result, interval_days, created_at
        FROM reviews WHERE problem_id = ? AND completed = 1
        ORDER BY id ASC
    """, (problem_id,))


def related(problem_id: int, subject: str) -> list[dict[str, Any]]:
    """同知识点 / 同课程的其他题目（排除自身，最多 3 题）。"""
    p = row("SELECT topic, course FROM problems WHERE id = ?", (problem_id,))
    if not p:
        raise ProblemsError("题目不存在", 404)
    topic = p["topic"] or ""
    course = p["course"] or ""
    return rows(
        "SELECT id, title, course, topic, mastery FROM problems WHERE id != ? AND subject = ? AND (topic = ? OR course = ?) ORDER BY id DESC LIMIT 3",
        (problem_id, subject, topic, course),
    )


def duplicates(content: str, topic: str, exclude: int) -> list[dict[str, Any]]:
    """C7 查重：按 topic 加权 + 内容 bigram 相似度，返回 top 5 相似题。

    阈值 0.35，topic 相同加 0.15（封顶 1.0），按相似度降序取前 5。
    """
    if not content:
        return []
    candidates = rows("SELECT id, title, topic, content FROM problems WHERE id <> ? ORDER BY id DESC LIMIT 300",
                      (exclude or 0,))
    scored = []
    for c in candidates:
        sim = similarity(content, c["content"])
        if topic and topic == c["topic"]:
            sim = min(1.0, sim + 0.15)
        if sim >= 0.35:
            scored.append({
                "id": c["id"], "title": c["title"], "topic": c["topic"],
                "similarity": round(sim, 2),
            })
    scored.sort(key=lambda x: -x["similarity"])
    return scored[:5]


def similarity(a: str, b: str) -> float:
    """C7 字符 bigram Jaccard 相似度（零依赖，中文/英文通用）。"""
    a = re.sub(r"\s+", "", a or "")
    b = re.sub(r"\s+", "", b or "")
    if not a or not b:
        return 0.0
    def bigrams(s: str) -> set[str]:
        return {s[i:i + 2] for i in range(max(0, len(s) - 1))} if len(s) > 1 else {s}
    ba, bb = bigrams(a), bigrams(b)
    if not ba and not bb:
        return 1.0
    union = ba | bb
    return len(ba & bb) / len(union) if union else 0.0


def create(subject: str, data: dict[str, Any]) -> int:
    """新建错题（含首条复习排期），返回 problem_id。

    幂等缓存由 handler 负责（那里才知道 X-Request-Id 与响应体）。
    """
    title = str(data.get("title", "")).strip()
    content = str(data.get("content", "")).strip()
    if not title or not content:
        raise ProblemsError("标题和题目内容不能为空", 400)
    stamp = now()
    error_type = normalize_error_type(data.get("error_type", "待诊断"))
    tags = json.dumps(data.get("tags", []), ensure_ascii=False)
    tags_status = "confirmed" if data.get("tags") else "none"
    methods = json.dumps(_as_str_list(data.get("methods")), ensure_ascii=False)
    # A2：显式 concept_ids 校验存在后落库；未提供则稍后自动绑定
    raw_concepts = data.get("concept_ids") or []
    if isinstance(raw_concepts, list):
        ids = [cid for cid in raw_concepts if isinstance(cid, int)]
        concept_csv = f",{','.join(str(cid) for cid in ids)}," if ids else ""
    else:
        concept_csv = ""
    with DB_LOCK, db() as conn:
        cursor = conn.execute("""
            INSERT INTO problems(title, course, topic, content, my_attempt, error_type,
                                 error_path, trap_note, shortcut, fix_action, tags, tags_status,
                                 concept_ids, media_path, methods, subject, mastery, ease_factor, repetition,
                                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 2.5, 0, ?, ?)
        """, (
            title, str(data.get("course", "")).strip(), str(data.get("topic", "")).strip(),
            content, str(data.get("my_attempt", "")).strip(), error_type,
            str(data.get("error_path", "")).strip(), str(data.get("trap_note", "")).strip(),
            str(data.get("shortcut", "")).strip(), str(data.get("fix_action", "")).strip(),
            tags, tags_status, concept_csv,
            normalize_media_paths(data.get("media_path", "")),
            methods,
            subject, clamp_mastery(int(data.get("mastery", 1))), stamp, stamp,
        ))
        problem_id = int(cursor.lastrowid)
        conn.execute(
            "INSERT INTO reviews(problem_id, due_date, interval_days, created_at) VALUES (?, ?, 1, ?)",
            (problem_id, (date.today() + timedelta(days=1)).isoformat(), stamp),
        )
    if not concept_csv:
        graph.bind_problem(problem_id)
    return problem_id


def update(problem_id: int, data: dict[str, Any]) -> None:
    """更新错题字段；tags/methods 仅显式提交才覆盖。"""
    existing = row("SELECT * FROM problems WHERE id = ?", (problem_id,))
    if not existing:
        raise ProblemsError("题目不存在", 404)
    fields = ["title", "course", "topic", "content", "my_attempt", "error_type",
              "error_path", "trap_note", "shortcut", "fix_action", "mastery", "starred"]
    merged = {field: data.get(field, existing[field]) for field in fields}
    merged["mastery"] = clamp_mastery(int(merged["mastery"]))
    merged["title"] = str(merged["title"]).strip()
    merged["content"] = str(merged["content"]).strip()
    merged["error_type"] = normalize_error_type(merged["error_type"])
    if not merged["title"] or not merged["content"]:
        raise ProblemsError("标题和题目内容不能为空", 400)
    # B5（R3）：tags 显式提交才算「草稿确认」，落库并置 confirmed；否则保留原状
    tags = existing["tags"]
    tags_status = existing["tags_status"]
    if data.get("tags") is not None:
        tags = json.dumps(data["tags"], ensure_ascii=False)
        tags_status = "confirmed"
    # A8：methods 显式提交则整体覆盖（结构校验：只收字符串数组）
    methods = existing["methods"]
    if data.get("methods") is not None:
        methods = json.dumps(_as_str_list(data["methods"]), ensure_ascii=False)
    content_fields = [f for f in fields if f != "mastery"]
    with DB_LOCK, db() as conn:
        conn.execute("""
            UPDATE problems SET title=?, course=?, topic=?, content=?, my_attempt=?, error_type=?,
                                error_path=?, trap_note=?, shortcut=?, fix_action=?,
                                starred=?, tags=?, tags_status=?, methods=?, updated_at=?
            WHERE id=?
        """, tuple(merged[field] for field in content_fields) + (tags, tags_status, methods, now(), problem_id))
        # ADR 0004：掌握度只经单一写入口（手动修改记 entry_point=edit，与算法结果可区分）
        set_problem_mastery(conn, problem_id, merged["mastery"], "edit", "用户手动编辑")


def batch(ids: Any, action: str) -> int:
    """批量删除/切换星标，返回影响条数。审计留在 handler（那里才知道客户端 IP）。"""
    if not isinstance(ids, list) or not ids or not action:
        raise ProblemsError("参数不合法 (ids/action)", 400)
    with DB_LOCK, db() as conn:
        for pid in ids:
            pid = int(pid)
            if action == "delete":
                conn.execute("DELETE FROM problems WHERE id = ?", (pid,))
            elif action == "star":
                conn.execute("UPDATE problems SET starred = CASE WHEN starred THEN 0 ELSE 1 END, updated_at = ? WHERE id = ?",
                             (now(), pid))
    return len(ids)


def import_data(data: dict[str, Any]) -> dict[str, Any]:
    """导入：先自动备份当前数据库，再参数化写入，避免注入与数据丢失。

    返回 {"imported": n, "backup": path}；备份失败抛 ProblemsError(500)。
    """
    problems = data.get("problems")
    if not isinstance(problems, list):
        raise ProblemsError("导入数据格式错误（缺少 problems 列表）", 400)
    # 版本兼容性校验（防未来 schema 不一致的备份被误加载）
    data_version = int(data.get("version", 1))
    if data_version > 1:
        raise ProblemsError(f"备份来自更新的版本 (v{data_version})，请升级应用后再导入", 400)
    # 自动备份到 backups/（与每日自动备份同目录，避免根目录堆积 .bak 文件）
    backup_dir = APP_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"import_{now().replace(':', '').replace('-', '')}.db"
    try:
        shutil.copy(db_module.DB_PATH, backup)
    except OSError as exc:
        raise ProblemsError(f"备份失败: {exc}", 500)
    # 只保留最近 7 份导入备份（防长期累积）
    try:
        olds = sorted(backup_dir.glob("import_*.db"))
        for old in olds[:-7]:
            old.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        with DB_LOCK, db() as conn:
            conn.execute("DELETE FROM hints")
            conn.execute("DELETE FROM reviews")
            conn.execute("DELETE FROM problems")
            for p in problems:
                if not isinstance(p, dict):
                    continue
                pid = int(p.get("id", 0))
                title = str(p.get("title", "")).strip()
                content = str(p.get("content", "")).strip()
                if not title or not content:
                    continue
                conn.execute("""
                    INSERT INTO problems(id, title, course, topic, content, my_attempt, error_type,
                                         error_path, trap_note, shortcut, fix_action,
                                         tags, tags_status,
                                         mastery, ease_factor, repetition, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 2.5, 0, ?, ?)
                """, (
                    pid, title, str(p.get("course", "")).strip(), str(p.get("topic", "")).strip(),
                    content, str(p.get("my_attempt", "")).strip(), normalize_error_type(p.get("error_type", "待诊断")),
                    str(p.get("error_path", "")).strip(), str(p.get("trap_note", "")).strip(),
                    str(p.get("shortcut", "")).strip(), str(p.get("fix_action", "")).strip(),
                    json.dumps(p.get("tags", []), ensure_ascii=False), str(p.get("tags_status", "none")).strip(),
                    clamp_mastery(int(p.get("mastery", 1))),
                    str(p.get("created_at", now())), str(p.get("updated_at", now())),
                ))
            # 导入提示记录
            for h in data.get("hints", []):
                if not isinstance(h, dict):
                    continue
                conn.execute(
                    "INSERT INTO hints(problem_id, level, content, created_at) VALUES (?, ?, ?, ?)",
                    (int(h.get("problem_id", 0)), int(h.get("level", 1)),
                     str(h.get("content", "")).strip(), str(h.get("created_at", now()))),
                )
            # 导入复习记录
            for rv in data.get("reviews", []):
                if not isinstance(rv, dict):
                    continue
                conn.execute(
                    "INSERT INTO reviews(problem_id, due_date, interval_days, result, completed, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (int(rv.get("problem_id", 0)), str(rv.get("due_date", "")).strip(),
                     int(rv.get("interval_days", 1)), str(rv.get("result", "")).strip(),
                     int(rv.get("completed", 0)), str(rv.get("created_at", now()))),
                )
    except (ValueError, KeyError) as exc:
        raise ProblemsError(f"导入失败: {exc}", 400)
    return {"imported": len(problems), "backup": str(backup)}


def log_variant_result(conn: Any, review: dict[str, Any], rating: int) -> None:
    """A4：变式复习后回写题根质量分（correct/total），低正确率变式自动降权。

    必须留在调用方的事务里（故收 conn，不自己开连接）。
    """
    vid = int(review.get("variant_id") or 0)
    if not vid:
        return
    p = conn.execute("SELECT variants FROM problems WHERE id = ?", (review["problem_id"],)).fetchone()
    if not p or not p["variants"]:
        return
    try:
        variants = json.loads(p["variants"])
    except json.JSONDecodeError:
        return
    idx = vid - 1
    if not (0 <= idx < len(variants)):
        return
    v = variants[idx]
    v["correct"] = int(v.get("correct", 0)) + (1 if rating >= 3 else 0)
    v["total"] = int(v.get("total", 0)) + 1
    conn.execute("UPDATE problems SET variants = ? WHERE id = ?",
                 (json.dumps(variants, ensure_ascii=False), review["problem_id"]))


def log_mastery(conn: Any, subject: str = "physics") -> None:
    """记录当日学科掌握度均值，每天每学科保留一条（供趋势图）。"""
    r = conn.execute("SELECT AVG(mastery) AS a, COUNT(*) AS c FROM problems WHERE subject = ?", (subject,)).fetchone()
    avg = round(r["a"] or 0, 2)
    today = date.today().isoformat()
    conn.execute("DELETE FROM mastery_log WHERE day = ? AND subject = ?", (today, subject))
    conn.execute(
        "INSERT INTO mastery_log(day, avg_mastery, count, subject) VALUES (?, ?, ?, ?)",
        (today, avg, r["c"] or 0, subject),
    )


def normalize_media_paths(value: Any) -> str:
    """拼接图片相对路径（逗号分隔，去空去重，仅允许 media/ 前缀）。"""
    if isinstance(value, list):
        parts = [str(p).strip() for p in value]
    else:
        parts = str(value or "").split(",")
    seen: list[str] = []
    for p in parts:
        p = p.strip().replace("\\", "/")
        if p.startswith("media/") and ".." not in p and p not in seen:
            seen.append(p)
    return ",".join(seen)
