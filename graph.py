"""A2 概念知识图谱：种子加载、掌握度传播、题-概念绑定、先修告警。

- 层级：单元(0) → 章(1) → 概念(2)，概念拥有 prerequisite/related/contrast 三种关系。
- 掌握度：problems 绑定概念的平均 mastery 为自身掌握度；沿 prerequisite 边做
  最多两轮「先修门」传播（先修不熟 → 子概念打折），并存概念进度表。
- 种子数据 data/seed_concepts.json 仅在库空时一次性加载；用户增删改标记
  user_edited=1，不回写 seed 文件（改动仅存库）。
"""
from __future__ import annotations

import json
import re
from typing import Any

from config import LOG, BUNDLE_ROOT
from db import db, now, row, rows, DB_LOCK

SEED_PATH = BUNDLE_ROOT / "data" / "seed_concepts.json"
PREREQ_THRESHOLD = 0.6
_LEVEL_UNIT = 0
_LEVEL_CHAPTER = 1
_LEVEL_CONCEPT = 2

# 学科：id -> 种子文件（physics 沿用旧文件名保持兼容；custom 学科无内置种子）
SUBJECT_SEEDS: dict[str, str] = {
    "physics": "seed_concepts.json",
    "chemistry": "seed_concepts_chemistry.json",
    "math": "seed_concepts_math.json",
}


def subject_seed_path(subject: str) -> Path:
    fname = SUBJECT_SEEDS.get(subject, f"seed_concepts_{subject}.json")
    return BUNDLE_ROOT / "data" / fname


def _load_seed(subject: str = "physics") -> dict[str, Any] | None:
    try:
        return json.loads(subject_seed_path(subject).read_text(encoding="utf-8"))
    except OSError as exc:
        LOG.warning("种子图谱文件不可读 (%s): %s", subject, exc)
        return None
    except json.JSONDecodeError as exc:
        LOG.warning("种子图谱文件解析失败 (%s): %s", subject, exc)
        return None


def ensure_seed(subject: str = "physics") -> None:
    """按学科从种子文件一次性加载（幂等；该学科已有概念则跳过）。"""
    with DB_LOCK, db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM concepts WHERE subject = ?", (subject,)
        ).fetchone()["c"]
        if count:
            return
        seed = _load_seed(subject)
        if not seed:
            return
        with conn:
            name_to_id: dict[str, int] = {}
            # 第一遍：插入全部节点（含单元/章节），建立名字索引
            for unit in seed.get("units", []):
                unit_id = _insert_concept(conn, unit["name"], 0, 0, 0.1, name_to_id, subject)
                for chapter in unit.get("chapters", []):
                    ch_id = _insert_concept(conn, chapter["name"], unit_id, unit_id, 0.2, name_to_id, subject)
                    for c in chapter.get("concepts", []):
                        cid = _insert_concept(conn, c["n"], ch_id, ch_id, float(c.get("d", 0.5)), name_to_id, subject)
                        name_to_id.setdefault(c["n"], cid)
            # 第二遍：建先修边（不依赖出现顺序）
            for unit in seed.get("units", []):
                for chapter in unit.get("chapters", []):
                    for c in chapter.get("concepts", []):
                        cid = name_to_id.get(c["n"])
                        if not cid:
                            continue
                        for prereq_name in c.get("p", []):
                            if prereq_name in name_to_id:
                                _insert_link(conn, name_to_id[prereq_name], cid, "prerequisite")
                            else:
                                LOG.warning("先修引用不存在，已跳过: %s", prereq_name)
            for relation, pairs in seed.get("links", {}).items():
                for a, b in pairs:
                    if a in name_to_id and b in name_to_id:
                        _insert_link(conn, name_to_id[a], name_to_id[b], relation)
                    else:
                        LOG.warning("%s 关系引用不存在，已跳过: %s-%s", relation, a, b)
        LOG.info("种子图谱已加载 (%s): %d 概念", subject, len(name_to_id))


def _insert_concept(conn: Any, name: str, parent_id: int, chapter_id: int,
                    difficulty: float, name_to_id: dict[str, int], subject: str = "physics") -> int:
    cur = conn.execute(
        "SELECT id FROM concepts WHERE subject = ? AND name = ?", (subject, name)
    ).fetchone()
    if cur:
        return int(cur["id"])
    cursor = conn.execute(
        "INSERT INTO concepts(name, parent_id, chapter_id, difficulty, subject, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (name, parent_id, chapter_id, difficulty, subject, now()),
    )
    cid = int(cursor.lastrowid)
    name_to_id[name] = cid
    return cid


def _insert_link(conn: Any, a: int, b: int, relation: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO concept_links(concept_a, concept_b, relation) VALUES (?, ?, ?)",
        (a, b, relation),
    )


def load_graph(subject: str = "physics") -> dict[str, Any]:
    """返回指定学科图谱（节点含层级/难度/掌握度，边含关系）。"""
    ensure_seed(subject)
    nodes = rows("SELECT * FROM concepts WHERE subject = ? ORDER BY id", (subject,))
    for n in nodes:
        n["level"] = _level_of(n)
    links = rows(
        "SELECT concept_a, concept_b, relation FROM concept_links "
        "WHERE concept_a IN (SELECT id FROM concepts WHERE subject = ?) "
        "ORDER BY CASE relation WHEN 'prerequisite' THEN 0 WHEN 'contrast' THEN 1 ELSE 2 END",
        (subject,),
    )
    return {"nodes": nodes, "links": links, "subject": subject}


def _level_of(node: dict[str, Any]) -> int:
    if node["parent_id"] == 0:
        return _LEVEL_UNIT
    parent = row("SELECT parent_id FROM concepts WHERE id = ?", (node["parent_id"],))
    if not parent:
        return _LEVEL_CONCEPT
    return _LEVEL_CHAPTER if parent["parent_id"] == 0 else _LEVEL_CONCEPT


def _linked_ids(concept_id: int, relation: str, reverse: bool = False) -> list[int]:
    col_a, col_b = ("concept_b", "concept_a") if reverse else ("concept_a", "concept_b")
    return [r[col_b] for r in rows(
        f"SELECT {col_b} FROM concept_links WHERE {col_a} = ? AND relation = ?", (concept_id, relation),
    )]


def _self_mastery(conn: Any, concept_id: int) -> tuple[float, int]:
    """该概念被绑定题目的平均掌握度（0-1）与绑定题数。"""
    subj = conn.execute("SELECT subject FROM concepts WHERE id = ?", (concept_id,)).fetchone()
    subject = subj["subject"] if subj else "physics"
    r = conn.execute("""
        SELECT AVG(mastery) / 5.0 AS m, COUNT(*) AS c FROM problems
        WHERE subject = ? AND concept_ids LIKE ?
    """, (subject, f"%,{concept_id},%")).fetchone()
    return float(r["m"] or 0.0), int(r["c"] or 0)


def concept_ids_to_list(raw: str) -> list[int]:
    """DB 内 concept_ids 为两侧逗号 CSV（如 ",1,7,"），解析为整数列表。"""
    try:
        return [int(x) for x in (raw or "").split(",") if x.strip().isdigit()]
    except (TypeError, ValueError):
        return []


def update_progress(subject: str = "physics") -> None:
    """重算掌握度：自身聚合 + 先修门传播两轮（A→B 表示 A 是先修）。"""
    ensure_seed(subject)
    with DB_LOCK, db() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM concepts WHERE parent_id <> 0 AND subject = ?", (subject,)
        ).fetchall()]
        self_m: dict[int, float] = {}
        for cid in ids:
            m, cnt = _self_mastery(conn, cid)
            self_m[cid] = m
            conn.execute(
                "INSERT OR REPLACE INTO concept_progress(concept_id, mastery, reviews, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (cid, m, cnt, now()),
            )
        links = conn.execute(
            "SELECT concept_a, concept_b FROM concept_links WHERE relation = 'prerequisite'"
        ).fetchall()
        prereq: dict[int, list[int]] = {}
        for l in links:
            prereq.setdefault(l["concept_b"], []).append(l["concept_a"])
        eff = dict(self_m)
        for _ in range(2):
            changed = False
            for cid in ids:
                parents = prereq.get(cid, [])
                if not parents:
                    continue
                gate = min(1.0, 0.5 + 0.5 * sum(eff.get(p, 0.0) for p in parents) / len(parents))
                new_val = self_m[cid] * gate
                if abs(new_val - eff[cid]) > 1e-6:
                    eff[cid] = new_val
                    changed = True
            if not changed:
                break
        for cid, val in eff.items():
            conn.execute(
                "UPDATE concept_progress SET mastery = ? WHERE concept_id = ?", (round(val, 3), cid),
            )


def _concept_ids_of(problem: dict[str, Any]) -> list[int]:
    return concept_ids_to_list(problem.get("concept_ids") or "")


def bind_problem(problem_id: int) -> list[int]:
    """题-概念绑定：本地关键词匹配（图谱概念名命中 topic>title>content）。

    绑定结果属于结构化校验（概念必须存在于图谱），直接落库不违反 R3。
    """
    ensure_seed()
    problem = row("SELECT * FROM problems WHERE id = ?", (problem_id,))
    if not problem:
        return []
    concept_ids = _local_bind(problem)
    csv = ",".join(f",{cid}," for cid in concept_ids) or ""
    with DB_LOCK, db() as conn:
        conn.execute("UPDATE problems SET concept_ids = ? WHERE id = ?", (csv, problem_id))
    update_progress()
    return concept_ids


def _local_bind(problem: dict[str, Any]) -> list[int]:
    """本地降级：题目 topic > title > content 依次做概念名包含匹配，命中即绑定。"""
    ensure_seed()
    subject = problem.get("subject") or "physics"
    text_fields = [problem.get("topic", ""), problem.get("title", ""), problem.get("content", "")]
    hits: list[int] = []
    for text in text_fields:
        if not text:
            continue
        for c in rows("SELECT id, name FROM concepts WHERE parent_id <> 0 AND subject = ?", (subject,)):
            if c["name"] and c["name"] in text:
                hits.append(int(c["id"]))
        if hits:
            break
    # 去重保序
    seen: set[int] = set()
    return [x for x in hits if not (x in seen or seen.add(x))]


def prereq_warnings(problem_id: int) -> list[dict[str, Any]]:
    """先修检测：返回绑定概念中「先修概念掌握度低」的告警列表。"""
    ensure_seed()
    problem = row("SELECT * FROM problems WHERE id = ?", (problem_id,))
    if not problem:
        return []
    warnings: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for cid in _concept_ids_of(problem):
        for prereq_id in _linked_ids(cid, "prerequisite", reverse=True):
            if (cid, prereq_id) in seen:
                continue
            seen.add((cid, prereq_id))
            p = row("SELECT * FROM concepts WHERE id = ?", (prereq_id,))
            if not p:
                continue
            prog = row("SELECT mastery FROM concept_progress WHERE concept_id = ?", (prereq_id,))
            mastery = prog["mastery"] if prog else (p["mastery_est"] or 0.0)
            if mastery < PREREQ_THRESHOLD:
                warnings.append({
                    "concept_id": prereq_id,
                    "name": p["name"],
                    "mastery": round(mastery * 100),
                })
    return warnings


def prereq_chain(concept_id: int) -> list[int]:
    """先修链（含自身与全部递归先修），用于「先修模式」过滤历史错题。"""
    ensure_seed()
    chain: list[int] = []
    stack = [concept_id]
    while stack:
        cid = stack.pop()
        if cid in chain:
            continue
        chain.append(cid)
        for p in _linked_ids(cid, "prerequisite", reverse=True):
            stack.append(p)
    return chain


def problems_for_concepts(concept_ids: list[int], limit: int = 100) -> list[dict[str, Any]]:
    """绑定到任一给定概念的题目列表（先修模式相关错题）。"""
    if not concept_ids:
        return []
    sql = " OR ".join("concept_ids LIKE ?" for _ in concept_ids)
    params = tuple(f"%,{cid},%" for cid in concept_ids)
    with DB_LOCK, db() as conn:
        cur = conn.execute(
            f"SELECT id, title, topic, mastery, error_type FROM problems "
            f"WHERE {sql} ORDER BY id DESC LIMIT {limit}",
            params,
        )
        return [dict(r) for r in cur.fetchall()]


def add_concept(name: str, parent_id: int = 0, subject: str = "physics") -> int | None:
    """用户新增概念（user_edited=1，不回写 seed）。parent_id 为章节点 id。"""
    name = str(name).strip()
    if not name:
        return None
    parent = None
    if parent_id:
        parent = row("SELECT * FROM concepts WHERE id = ?", (parent_id,))
        if parent and parent.get("subject") != subject:
            return None
    chapter_id = 0
    if parent:
        if _level_of(parent) == _LEVEL_CHAPTER:
            chapter_id = parent["id"]
        else:
            chapter_id = parent["chapter_id"]
    with DB_LOCK, db() as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO concepts(name, parent_id, chapter_id, difficulty, user_edited, created_at, subject) "
                "VALUES (?, ?, ?, 0.5, 1, ?, ?)",
                (name, parent_id, chapter_id, now(), subject),
            )
        except Exception as exc:
            LOG.warning("新增概念失败（可能重名）: %s", exc)
            return None
        return int(cursor.lastrowid)


def delete_concept(concept_id: int) -> bool:
    """删除概念（级联删除其关系边；chapter/unit 级需先确认）。"""
    with DB_LOCK, db() as conn:
        node = conn.execute("SELECT * FROM concepts WHERE id = ?", (concept_id,)).fetchone()
        if not node:
            return False
        children = conn.execute("SELECT COUNT(*) AS c FROM concepts WHERE parent_id = ?", (concept_id,)).fetchone()["c"]
        if children:
            return False
        bound = conn.execute("SELECT COUNT(*) AS c FROM problems WHERE concept_ids LIKE ?", (f"%,{concept_id},%",)).fetchone()["c"]
        with conn:
            conn.execute("DELETE FROM concept_links WHERE concept_a = ? OR concept_b = ?", (concept_id, concept_id))
            conn.execute("DELETE FROM concept_progress WHERE concept_id = ?", (concept_id,))
            conn.execute("DELETE FROM concepts WHERE id = ?", (concept_id,))
        return True
