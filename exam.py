"""B4 真题对齐 + 考试就绪度。

就绪度 = Σ(真题考点权重 × 该考点错题平均掌握度/5) / Σ权重。
真题命中度 = 考点 topic 在错题本中出现过的比例（衡量「刷到的题是否覆盖考点」）。
分数预测（§33.2）= 就绪度 × 考试日可提取率折扣，附带置信区间。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
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


def _exam_day(exam_date: str | None) -> date:
    """解析考试日；非法或空则回退为今天 + 7 天（典型模考窗口）。"""
    if exam_date:
        try:
            return date.fromisoformat(exam_date[:10])
        except ValueError:
            pass
    return date.today() + timedelta(days=7)


def predict_score(paper_id: int, exam_date: str | None = None) -> dict[str, Any]:
    """§33.2 分数预测：就绪度 × 考试日可提取率折扣，输出点位预测与置信区间。

    模型（透明可解释，无外部依赖）：
      retro = 试卷覆盖题目的平均可提取概率（FSRS 在考试日的 R；不可用时取 1.0 并标注 proxy）
      predicted = readiness × (0.6 + 0.4 × retro)   # 就绪度已含掌握度，retro 仅做遗忘折扣
    置信区间宽度随 FSRS 训练样本量收缩（confidence_for）。
    """
    from fsrs_bridge import retrievability, fsrs_status, confidence_for

    paper = row("SELECT * FROM exam_papers WHERE id = ?", (paper_id,))
    if not paper:
        return {}
    base = paper_readiness(paper_id)
    if not base:
        return {}
    readiness = float(base["readiness"])
    eday = _exam_day(exam_date or paper.get("exam_date") or None)
    days_ahead = max(0, (eday - date.today()).days)

    # 试卷覆盖的 topic 集合 → 取这些题目计算考试日平均可提取率
    topics = {q["topic"] for q in base["questions"] if q["topic"]}
    retro = 1.0
    method = "proxy"  # 无 FSRS 时仅按就绪度乐观估计
    if topics:
        placeholders = ",".join("?" for _ in topics)
        probs = rows(
            f"SELECT id, state, stability, difficulty, updated_at FROM problems "
            f"WHERE topic IN ({placeholders}) AND mastery >= 1",
            tuple(topics),
        )
        if probs:
            total = 0.0
            n = 0
            for p in probs:
                r = retrievability(
                    prev_interval=max(1, int(p["stability"]) if p["stability"] else 1),
                    state=int(p["state"] or 0),
                    stability=float(p["stability"] or 0.0),
                    difficulty=float(p["difficulty"] or 0.0),
                    last_review=str(p["updated_at"] or ""),
                    current=eday,
                )
                total += r
                n += 1
            if n:
                retro = round(total / n, 4)
                method = "fsrs" if fsrs_status()["available"] else "proxy"

    predicted = round(readiness * (0.6 + 0.4 * retro), 1)

    # 置信区间：训练样本越少区间越宽
    sample_count = int(fsrs_status().get("sample_count", 0))
    band = {"insufficient": 18.0, "low": 12.0, "medium": 7.0, "high": 4.0}[confidence_for(sample_count)]
    lo = max(0.0, predicted - band)
    hi = min(100.0, predicted + band)
    return {
        "paper_id": paper_id,
        "exam_date": eday.isoformat(),
        "days_ahead": days_ahead,
        "readiness": readiness,
        "retrievability": retro,
        "method": method,
        "predicted": predicted,
        "lower": round(lo, 1),
        "upper": round(hi, 1),
        "target": float(base["target"]),
        "gap_to_target": round(max(0.0, base["target"] - predicted), 1),
        "confidence": confidence_for(sample_count),
    }
