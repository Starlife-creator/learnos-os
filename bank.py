"""题库模块：内置物理练习题库 + 答题判分 + 答错自动入错题库。

- 题库内容来自 data/seed_questions.json（只读，不落库）。
- 答题记录/进度存入 bank_attempts / bank_problems 表。
- 答错时自动在错题库（problems 表）建档并安排明日复习；答对仅记录进度。
零第三方依赖；题库文件缺失时整体降级为空题库。
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from config import LOG
from db import DB_LOCK, db, now, rows

BANK_FILE = Path(__file__).resolve().parent / "data" / "seed_questions.json"
CUSTOM_FILE = Path(__file__).resolve().parent / "data" / "bank_custom.json"
_BANK: dict[str, dict[str, Any]] = {}
"""学科 -> 题库缓存（内置 + 用户自定义）。"""

# 学科：id -> 内置题库文件名（physics 沿用旧文件名保持兼容）
SUBJECT_BANKS: dict[str, str] = {
    "physics": "seed_questions.json",
    "chemistry": "seed_questions_chemistry.json",
    "math": "seed_questions_math.json",
}

# 非公开字段（列表展示时不下发给前端，避免答案提前泄露）
_PRIVATE_FIELDS = ("answer", "explain")


def _bank_file(subject: str) -> Path:
    fname = SUBJECT_BANKS.get(subject, f"seed_questions_{subject}.json")
    return Path(__file__).resolve().parent / "data" / fname


def _custom_file(subject: str) -> Path:
    if subject == "physics":
        return CUSTOM_FILE  # 旧文件沿用，避免自定义题丢失
    return Path(__file__).resolve().parent / "data" / f"bank_custom_{subject}.json"


def load_bank(subject: str = "physics") -> dict[str, Any]:
    """加载指定学科题库（内置题库 + 用户自定义题库）。文件缺失/损坏时降级为空题库。"""
    global _BANK
    if subject not in _BANK:
        seed: dict[str, Any] = {"version": 0, "questions": []}
        try:
            seed = json.loads(_bank_file(subject).read_text("utf-8"))
            if not isinstance(seed, dict) or not isinstance(seed.get("questions"), list):
                raise ValueError("题库格式错误")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            LOG.warning("题库文件不可用（%s），题库功能降级为空: %s", subject, exc)
            seed = {"version": 0, "questions": []}
        custom = _load_custom_questions(subject)
        merged = {"version": seed.get("version", 0), "subject": subject, "questions": []}
        by_id: dict[str, dict[str, Any]] = {}
        for q in seed["questions"]:
            q = dict(q)
            q.setdefault("subject", subject)
            by_id.setdefault(str(q.get("id")), q)
        for q in custom:
            q = dict(q)
            q.setdefault("subject", subject)
            by_id[str(q.get("id"))] = q  # 自定义题覆盖同 id 的内置题
        merged["questions"] = list(by_id.values())
        _BANK[subject] = merged
    return _BANK[subject]


def _load_custom_questions(subject: str = "physics") -> list[dict[str, Any]]:
    try:
        data = json.loads(_custom_file(subject).read_text("utf-8"))
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("questions"), list):
            return data["questions"]
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return []


def _write_custom(questions: list[dict[str, Any]], subject: str = "physics") -> None:
    payload = {"version": 1, "subject": subject, "questions": questions}
    target = _custom_file(subject)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, target)


def import_questions(items: Any, subject: str = "physics") -> dict[str, Any]:
    """批量导入题目（JSON 数组）。逐条校验，合法者入库，返回结果统计。"""
    if not isinstance(items, list):
        raise ValueError("导入内容必须是题目数组")
    existing = load_bank(subject)
    used_ids = {str(q.get("id")) for q in existing["questions"]}
    customs = _load_custom_questions(subject)
    by_id = {str(q.get("id")): q for q in customs}
    imported = 0
    errors: list[str] = []
    for idx, raw in enumerate(items, 1):
        try:
            item = _normalize_item(raw, idx, used_ids, subject)
            qid = str(item["id"])
            by_id[qid] = item
            used_ids.add(qid)
            imported += 1
        except ValueError as exc:
            errors.append(f"第 {idx} 条: {exc}")
    _write_custom(list(by_id.values()), subject)
    _BANK.pop(subject, None)
    return {"imported": imported, "errors": errors}


def _normalize_item(raw: Any, idx: int, used_ids: set[str], subject: str = "physics") -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("必须为对象")
    stem = str(raw.get("stem") or raw.get("question") or "").strip()
    if len(stem) < 5:
        raise ValueError("题干过短")
    choices = raw.get("choices")
    if not isinstance(choices, list) or len(choices) < 2 or any(str(c).strip() == "" for c in choices):
        raise ValueError("choices 至少 2 个且非空")
    choices = [str(c).strip() for c in choices]
    try:
        answer = int(raw.get("answer"))
    except (TypeError, ValueError):
        raise ValueError("answer 必须是选项下标")
    if not (0 <= answer < len(choices)):
        raise ValueError("answer 下标越界")
    raw_id = str(raw.get("id") or "").strip()
    if not raw_id:
        raw_id = f"custom-{idx}"
    raw_id = re.sub(r"[^0-9A-Za-z_\-]", "-", raw_id)[:60]
    if raw_id in used_ids and raw_id.startswith("custom-"):
        raw_id = f"custom-{idx}-{raw_id}"
    item = {
        "id": raw_id,
        "subject": str(raw.get("subject") or subject).strip()[:20] or subject,
        "unit": str(raw.get("unit") or "未分类").strip()[:20] or "未分类",
        "chapter": str(raw.get("chapter") or "自选题").strip()[:30] or "自选题",
        "concept": str(raw.get("concept") or "其他").strip()[:40] or "其他",
        "difficulty": int(raw.get("difficulty") or 2),
        "stem": stem,
        "choices": choices,
        "answer": answer,
        "title": str(raw.get("title") or "").strip(),
        "explain": str(raw.get("explain") or "").strip(),
    }
    if not (1 <= item["difficulty"] <= 5):
        item["difficulty"] = 2
    return item


def _attempt_stats() -> dict[str, list[tuple[int, str]]]:
    """每个 qid 的答题记录（按时间升序），用于推导当前状态。"""
    out: dict[str, list[tuple[int, str]]] = {}
    for r in rows("SELECT qid, correct, attempted_at FROM bank_attempts ORDER BY id"):
        out.setdefault(r["qid"], []).append((r["correct"], r["attempted_at"]))
    return out


def _status_of(qid: str, stats: dict[str, list[tuple[int, str]]]) -> str:
    """状态：todo 未做 / wrong 答错（已入错题库） / done 最近答对。"""
    recs = stats.get(qid)
    if not recs:
        return "todo"
    return "done" if recs[-1][0] else "wrong"


def stats(subject: str = "physics") -> dict[str, int]:
    bank = load_bank(subject)
    st = _attempt_stats()
    total = len(bank["questions"])
    done = wrong = 0
    for q in bank["questions"]:
        s = _status_of(q["id"], st)
        if s == "done":
            done += 1
        elif s == "wrong":
            wrong += 1
    return {"total": total, "done": done, "wrong": wrong, "todo": max(0, total - done - wrong)}


def units(subject: str = "physics") -> list[dict[str, Any]]:
    """单元列表 + 各自进度统计（供前端筛选与展示）。"""
    bank = load_bank(subject)
    st = _attempt_stats()
    groups: dict[str, dict[str, int]] = {}
    for q in bank["questions"]:
        u = q.get("unit") or "未分类"
        g = groups.setdefault(u, {"count": 0, "done": 0, "wrong": 0})
        g["count"] += 1
        s = _status_of(q["id"], st)
        if s == "done":
            g["done"] += 1
        elif s == "wrong":
            g["wrong"] += 1
    return [{"unit": k, **v} for k, v in groups.items()]


def list_questions(unit: str = "", status: str = "all", q: str = "",
                   subject: str = "physics") -> list[dict[str, Any]]:
    """筛选后的题目列表。answer/explain 不下发（练习时由判分接口返回）。"""
    bank = load_bank(subject)
    st = _attempt_stats()
    kw = (q or "").strip().lower()
    out: list[dict[str, Any]] = []
    for item in bank["questions"]:
        if unit and item.get("unit") != unit:
            continue
        s = _status_of(item["id"], st)
        if status == "todo" and s != "todo":
            continue
        if status == "wrong" and s != "wrong":
            continue
        if status == "done" and s != "done":
            continue
        if kw:
            hay = (item.get("stem", "") + item.get("chapter", "") + item.get("concept", "")).lower()
            if kw not in hay:
                continue
        pub = {k: v for k, v in item.items() if k not in _PRIVATE_FIELDS}
        pub["status"] = s
        out.append(pub)
    return out


def _ensure_problem(conn: Any, item: dict[str, Any]) -> int:
    """答错时：把题目写入错题库（幂等——同一 qid 只建一条），并安排明日复习。"""
    existing = conn.execute(
        "SELECT problem_id FROM bank_problems WHERE qid = ?", (item["id"],)
    ).fetchone()
    due = (date.today() + timedelta(days=1)).isoformat()
    if existing:
        pid = int(existing[0])
        # 再次答错：掌握度回到 1，且若无待复习任务则补排一次
        conn.execute("UPDATE problems SET mastery = 1, updated_at = ? WHERE id = ?", (now(), pid))
        pending = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE problem_id = ? AND completed = 0", (pid,)
        ).fetchone()[0]
        if not pending:
            conn.execute(
                "INSERT INTO reviews(problem_id, due_date, interval_days, created_at) VALUES (?, ?, 1, ?)",
                (pid, due, now()),
            )
        return pid

    title = str(item.get("title") or "") or str(item["stem"])[:24]
    subject = str(item.get("subject") or "physics")
    cid = conn.execute(
        "SELECT id FROM concepts WHERE subject = ? AND name = ?", (subject, item.get("concept", ""))
    ).fetchone()
    concept_csv = f",{int(cid[0])}," if cid else ""
    stamp = now()
    cursor = conn.execute("""
        INSERT INTO problems(title, course, topic, content, my_attempt, error_type,
                             concept_ids, methods, mastery, ease_factor, repetition, created_at, updated_at, subject)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 2.5, 0, ?, ?, ?)
    """, (
        title,
        str(item.get("unit", "")),
        str(item.get("chapter", "")),
        str(item["stem"]),
        "",
        "待诊断",
        concept_csv,
        "[]",
        1,
        stamp,
        stamp,
        subject,
    ))
    pid = int(cursor.lastrowid)
    conn.execute(
        "INSERT INTO reviews(problem_id, due_date, interval_days, created_at) VALUES (?, ?, 1, ?)",
        (pid, due, stamp),
    )
    conn.execute(
        "INSERT OR REPLACE INTO bank_problems(qid, problem_id, updated_at, subject) VALUES (?, ?, ?, ?)",
        (item["id"], pid, stamp, subject),
    )
    return pid


def _grade_answer(user_raw: Any, correct_raw: Any) -> bool:
    """判分核心：数值学科按数值比较（容忍浮点/千分位/负号），否则归一化字符串比较。
    修复原 int() 强转对非数值学科（语言/历史/编程/化学符号/生物）一律判错或崩溃的缺陷。"""
    def _to_num(s: Any):
        try:
            return float(str(s).strip().replace(",", "").replace("，", "").replace(" ", ""))
        except (TypeError, ValueError):
            return None

    cu, cc = _to_num(user_raw), _to_num(correct_raw)
    if cu is not None and cc is not None:
        return abs(cu - cc) < 1e-6

    def _norm(s: Any) -> str:
        return re.sub(r"[\s\.。，,、；;:：]+$", "", str(s).strip().lower())

    return _norm(user_raw) == _norm(correct_raw)


def judge(qid: str, answer: Any, subject: str = "physics") -> dict[str, Any]:
    """判分。答错时自动建档入错题库。answer 越界视为错误。"""
    bank = load_bank(subject)
    item = next((q for q in bank["questions"] if q["id"] == qid), None)
    if not item:
        # 学科不匹配时回退：全学科查找（qid 全局唯一）
        for s, bk in _BANK.items():
            item = next((q for q in bk["questions"] if q["id"] == qid), None)
            if item:
                break
    if not item:
        raise ValueError("题目不存在")
    correct = _grade_answer(answer, item["answer"])
    problem_id = 0
    with DB_LOCK, db() as conn:
        conn.execute(
            "INSERT INTO bank_attempts(qid, correct, attempted_at) VALUES (?, ?, ?)",
            (qid, 1 if correct else 0, now()),
        )
        if not correct:
            problem_id = _ensure_problem(conn, item)
    return {
        "correct": correct,
        "answer": item["answer"],
        "explain": item["explain"],
        "problem_id": problem_id,
    }