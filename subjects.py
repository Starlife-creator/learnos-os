"""学科领域层：学科总览、新增学科、按 scope 删除、全局搜索（无 HTTP 知识）。

ADR 0010 第四批：`handler.py` 里与学科容器直接相关的读写下沉到这里。handler 侧只剩
「取 subject → 调本模块 → 写响应」，审计（需要客户端 IP）与 LOG 留在 HTTP 层。

删除学科是**多表级联**：scope 决定删哪些维度（graph / bank / personal / full），
表名来自本模块的硬编码白名单并再过一道 `safe_ident`，确保拼接的标识符不可被外部
输入影响。
"""
from __future__ import annotations

import re
from typing import Any

from db import DB_LOCK, db, list_subjects, now, row, rows, safe_ident, subject_exists
from domain_errors import DomainError

# 与前端新建学科表单的约定一致：字母开头，仅字母/数字/下划线，最长 20
SUBJECT_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,19}")

# scope → 待删表（按依赖顺序：先子后父）
SCOPE_TABLES: dict[str, list[str]] = {
    # 图谱维度：概念节点 + 先修/关联边 + 种子版本标记
    "graph": ["concept_links", "concepts", "seed_versions"],
    # 题库维度：题目 + 评分
    "bank": ["bank_scores", "bank_problems"],
    # 个人学习数据维度：错题本/口试 + 掌握度日志 + 打卡
    "personal": ["study_checkins", "mastery_log", "problems"],
}
FULL_TABLES = ["concept_links", "concepts", "seed_versions",
               "bank_scores", "bank_problems",
               "study_checkins", "mastery_log", "problems", "subjects"]
SCOPES = ("full", "graph", "bank", "personal")


class SubjectsError(DomainError):
    """学科领域层 4xx 语义：handler 据此输出 {"error": message} + status。"""


def overview() -> list[dict[str, Any]]:
    """学科列表 + 每科统计（概念数、题库题数、种子图谱状态）。

    前端据此对「无图谱/无题库」学科显示明确空状态徽标，而不是静默空白。
    """
    import bank
    import graph
    result = list_subjects()
    for s in result:
        sid = s["id"]
        s.setdefault("title", sid)
        if not s["title"]:
            s["title"] = sid
        with DB_LOCK, db() as conn:
            s["concept_count"] = conn.execute(
                "SELECT COUNT(*) FROM concepts WHERE subject = ?", (sid,)
            ).fetchone()[0]
        try:
            s["question_count"] = len(bank.load_bank(sid).get("questions", []))
        except Exception:  # 题库损坏不应让整个学科列表 500
            s["question_count"] = 0
        st = graph.seed_status(sid)
        s["has_seed_graph"] = st["has_seed_file"]
        s["seed_needs_update"] = st["needs_update"]
    return result


def add(sid: Any, title: Any = "") -> dict[str, Any]:
    """新增学科：校验 id、写入注册表、若有种子文件则自动加载图谱。"""
    sid = str(sid or "").strip().lower()
    title = str(title or "").strip() or sid
    if not SUBJECT_ID_RE.fullmatch(sid):
        raise SubjectsError("学科 id 需字母开头，仅限字母/数字/下划线，最长 20 字符")
    if subject_exists(sid):
        raise SubjectsError(f"学科 {sid} 已存在", 409)
    with DB_LOCK, db() as conn:
        conn.execute(
            "INSERT INTO subjects(id, title, builtin, created_at) VALUES (?, ?, 0, ?)",
            (sid, title, now()),
        )
    import graph
    graph.ensure_seed(sid)
    return {"id": sid, "title": title, "builtin": False}


def delete(subject_id: str, scope: str = "full") -> dict[str, Any]:
    """按 scope 删除学科数据。

    - scope=full（默认）：删该学科一切（subjects + 全部维度），等同于注销学科。
    - scope=graph：只删图谱（concepts/concept_links/seed_versions），保留题库与个人数据。
    - scope=bank：只删题库（bank_problems/bank_scores）。
    - scope=personal：只删个人学习数据（problems/mastery_log/study_checkins）。

    内置学科（builtin）任何 scope 均拒绝。full 以外的 scope 不删 subjects 行本身
    （学科容器保留，仅清空所选维度）；仅 full 才会移除学科注册。
    """
    info = row("SELECT id, builtin FROM subjects WHERE id = ?", (subject_id,))
    if not info:
        raise SubjectsError("学科不存在", 404)
    if info["builtin"]:
        raise SubjectsError("内置学科不可删除", 400)
    scope = str(scope or "full").strip().lower()
    if scope not in SCOPES:
        raise SubjectsError("未知 scope，仅支持 full/graph/bank/personal", 400)
    tables = FULL_TABLES if scope == "full" else SCOPE_TABLES[scope]

    deleted: dict[str, int] = {}
    with DB_LOCK, db() as conn:
        for tbl in tables:
            # 纵深防御：tbl 本就来自本模块的硬编码白名单，拼接前仍断言标识符合法，
            # 防未来有人把用户可控表名引入此路径造成注入。
            tbl = safe_ident(tbl)
            if tbl == "subjects":
                cur = conn.execute("DELETE FROM subjects WHERE id = ?", (subject_id,))
            elif tbl == "concept_links":
                # concept_links 无 subject 列，经 concepts 子查询定位该学科节点
                cur = conn.execute(
                    "DELETE FROM concept_links WHERE concept_a IN "
                    "(SELECT id FROM concepts WHERE subject = ?) "
                    "OR concept_b IN (SELECT id FROM concepts WHERE subject = ?)",
                    (subject_id, subject_id))
            else:
                cur = conn.execute(f"DELETE FROM {tbl} WHERE subject = ?", (subject_id,))
            deleted[tbl] = cur.rowcount
    return {"scope": scope, "deleted": deleted}


def _bank_hits(subject: str, q: str, limit: int) -> list[dict[str, Any]]:
    """题库是 JSON 文件而非表，只能逐题匹配题干/概念（composite 递归并入子题）。"""
    import bank

    def haystack(item: dict[str, Any]) -> str:
        hay = str(item.get("stem", "")) + " " + str(item.get("concept", ""))
        for part in item.get("parts") or []:
            hay += " " + haystack(part)
        return hay.lower()

    hits: list[dict[str, Any]] = []
    for question in bank.load_bank(subject).get("questions", []):
        if q.lower() not in haystack(question):
            continue
        stem = str(question.get("stem", "")).strip()
        if not stem:  # composite 引导语为空时用首个子题题干展示
            for part in question.get("parts") or []:
                stem = str(part.get("stem", "")).strip()
                if stem:
                    break
        hits.append({"id": question.get("id", ""), "stem": stem[:80],
                     "concept": question.get("concept", ""),
                     "type": question.get("type", "single")})
        if len(hits) >= limit:
            break
    return hits


def _doc_hits(q: str, limit: int) -> list[dict[str, Any]]:
    import rag
    return [{"name": hit.get("name", ""), "path": hit.get("source_path", ""),
             "page": hit.get("page", 0)}
            for hit in rag.search(q, k=max(1, limit - 3))]


def search(subject: str, query: str, limit: int = 6) -> dict[str, Any]:
    """全局搜索（Ctrl+K）：跨错题/概念/题库/RAG 文档，各组最多 limit 条。"""
    q = str(query or "").strip()
    if len(q) < 1:
        return {"problems": [], "concepts": [], "bank": [], "docs": []}
    like = f"%{q}%"
    problems = rows(
        "SELECT id, title, topic, mastery FROM problems "
        "WHERE subject = ? AND (title LIKE ? OR content LIKE ? OR topic LIKE ?) "
        "ORDER BY updated_at DESC LIMIT ?",
        (subject, like, like, like, limit))
    concepts = rows(
        "SELECT c.id, c.name FROM concepts c "
        "LEFT JOIN concept_progress cp ON cp.concept_id = c.id "
        "WHERE c.subject = ? AND c.name LIKE ? "
        "ORDER BY COALESCE(cp.mastery, 0) ASC, c.id LIMIT ?", (subject, like, limit))
    try:
        bank_hits = _bank_hits(subject, q, limit)
    except Exception:  # 题库缺失/损坏：搜索仍要能回结果
        bank_hits = []
    try:
        docs = _doc_hits(q, limit)
    except Exception:  # 未启用向量检索
        docs = []
    return {"problems": problems, "concepts": concepts, "bank": bank_hits, "docs": docs}
