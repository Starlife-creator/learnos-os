"""B4 真题对齐 + 考试就绪度。

就绪度 = Σ(真题考点权重 × 该考点错题平均掌握度/5) / Σ权重。
真题命中度 = 考点 topic 在错题本中出现过的比例（衡量「刷到的题是否覆盖考点」）。
"""
from __future__ import annotations

import json
from typing import Any

from db import DB_LOCK, db, now, row, rows


def _topic_mastery(conn: Any) -> dict[str, float]:
    """每个 topic 的平均掌握度（0-1），来自 problems 表。"""
    out: dict[str, float] = {}
    for r in conn.execute(
        "SELECT topic, AVG(CAST(mastery AS REAL)) AS m FROM problems "
        "WHERE topic != '' GROUP BY topic"
    ).fetchall():
        if r["m"] is not None:
            out[r["topic"]] = min(1.0, max(0.0, float(r["m"]) / 5.0))
    return out


def paper_readiness(paper_id: int) -> dict[str, Any]:
    """单卷就绪度：就绪度、命中度、未覆盖考点、差值。"""
    with db() as conn:
        paper = row("SELECT * FROM exam_papers WHERE id = ?", (paper_id,))
        if not paper:
            return {}
        questions = rows("SELECT * FROM exam_questions WHERE paper_id = ? ORDER BY id", (paper_id,))
        for q in questions:
            try:
                q["related_problems"] = json.loads(q.get("related_problems") or "[]")
            except json.JSONDecodeError:
                q["related_problems"] = []
        mastery = _topic_mastery(conn)
        total_w = sum(q["weight"] for q in questions) or 1.0
        ready_w = sum(q["weight"] * mastery.get(q["topic"], 0.0) for q in questions)
        covered = {q["topic"] for q in questions if q["topic"] in mastery}
        topics = {q["topic"] for q in questions if q["topic"]}
        hit_rate = len(covered & topics) / len(topics) if topics else 0.0
        readiness = ready_w / total_w * 100.0
        gaps = sorted({q["topic"] for q in questions if mastery.get(q["topic"], 0.0) < 0.6})
        return {
            "paper": dict(paper),
            "question_count": len(questions),
            "readiness": round(readiness, 1),
            "gap_to_target": round(max(0.0, paper["target"] - readiness), 1),
            "target": paper["target"],
            "hit_rate": round(hit_rate * 100.0, 1),
            "gaps": sorted(gaps),
            "questions": questions,
        }


def overall_readiness() -> dict[str, Any]:
    """全局：所有试卷就绪度聚合（未建卷时为 None）。"""
    papers = rows("SELECT id FROM exam_papers ORDER BY id")
    if not papers:
        return {"papers": [], "overall": None}
    items = [paper_readiness(p["id"]) for p in papers]
    if not items:
        return {"papers": [], "overall": None}
    avg = sum(i["readiness"] for i in items) / len(items)
    return {"papers": items, "overall": round(avg, 1)}


def create_paper(name: str, exam_date: str = "", target: float = 80) -> int:
    with DB_LOCK, db() as conn:
        cur = conn.execute(
            "INSERT INTO exam_papers(name, exam_date, target, created_at) VALUES (?, ?, ?, ?)",
            (name.strip(), exam_date.strip(), max(1.0, min(100.0, target)), now()),
        )
        return int(cur.lastrowid)


def add_questions(paper_id: int, questions: list[dict[str, Any]]) -> int:
    """批量添加真题题目；related_problems 为错题 id 列表（互链）。"""
    cleaned = []
    for q in questions[:200]:
        topic = str(q.get("topic", "")).strip()
        if not topic:
            continue
        try:
            weight = float(q.get("weight", 1))
        except (TypeError, ValueError):
            weight = 1.0
        rel = q.get("related_problems") or []
        if isinstance(rel, list):
            rel = [int(x) for x in rel if str(x).isdigit()]
        cleaned.append((
            q.get("qno", ""), topic, max(0.5, min(5.0, weight)),
            str(q.get("content", "")).strip(),
            json.dumps(rel, ensure_ascii=False),
        ))
    if not cleaned:
        return 0
    with DB_LOCK, db() as conn:
        conn.executemany(
            "INSERT INTO exam_questions(paper_id, qno, topic, weight, content, related_problems, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(paper_id, *c, now()) for c in cleaned],
        )
        return len(cleaned)


def delete_paper(paper_id: int) -> bool:
    with DB_LOCK, db() as conn:
        cur = conn.execute("DELETE FROM exam_papers WHERE id = ?", (paper_id,))
        return cur.rowcount > 0
