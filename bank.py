"""题库模块：内置练习题库 + 答题判分 + 答错自动入错题库。

- 题库内容来自 data/seed_questions_<id>.json（只读，不落库）。
- 答题记录/进度存入 bank_attempts / bank_problems 表。
- 答错时自动在错题库（problems 表）建档并安排明日复习；答对仅记录进度。
零第三方依赖；题库文件缺失时整体降级为空题库。
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from config import LOG
from db import DB_LOCK, db, now, rows

CUSTOM_FILE = Path(__file__).resolve().parent / "data" / "bank_custom.json"
_BANK: dict[str, dict[str, Any]] = {}
"""学科 -> 题库缓存（内置 + 用户自定义）。"""
_BANK_LOCK = threading.RLock()  # C1e：双端保护 _BANK（load/import 写 + judge 迭代读），RLock 防未来嵌套

# 所有学科（含内置三科）统一使用 seed_questions_<id>.json；physics 旧用 seed_questions.json，保留兼容读取。
SUBJECT_BANKS: dict[str, str] = {
    "physics": "seed_questions_physics.json",
    "chemistry": "seed_questions_chemistry.json",
    "math": "seed_questions_math.json",
}

# 非公开字段（列表展示时不下发给前端，避免答案提前泄露）
_PRIVATE_FIELDS = ("answer", "explain")

# 题型：single 单选 / multiple 多选 / fill 填空 / subjective 主观 / composite 大小题（含子题）
QUESTION_TYPES = ("single", "multiple", "fill", "subjective", "composite")
_TYPE_LABEL_ZH = {
    "single": "单选",
    "multiple": "多选",
    "fill": "填空",
    "subjective": "主观题",
    "composite": "大小题",
}


def _bank_file(subject: str) -> Path:
    fname = SUBJECT_BANKS.get(subject, f"seed_questions_{subject}.json")
    return Path(__file__).resolve().parent / "data" / fname


def _custom_file(subject: str) -> Path:
    """用户自定义题库路径：统一为 bank_custom_<id>.json。"""
    return Path(__file__).resolve().parent / "data" / f"bank_custom_{subject}.json"


def _legacy_custom_file(subject: str) -> Path | None:
    """仅 physics：返回旧 bank_custom.json 路径（若存在），用于兼容读取。"""
    if subject != "physics":
        return None
    return CUSTOM_FILE if CUSTOM_FILE.is_file() else None


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
        with _BANK_LOCK:
            _BANK[subject] = merged
    return _BANK[subject]


def find_question(qid: str, subject: str = "physics") -> dict[str, Any]:
    """按 qid 查题（含跨学科回退，qid 应全局唯一）。找不到抛 ValueError。"""
    bank = load_bank(subject)
    item = next((q for q in bank["questions"] if str(q.get("id")) == str(qid)), None)
    if not item:
        for s, bk in _BANK.items():
            if s == subject:
                continue
            item = next((q for q in bk["questions"] if str(q.get("id")) == str(qid)), None)
            if item:
                break
    if not item:
        raise ValueError("题目不存在")
    return item


def _load_custom_questions(subject: str = "physics") -> list[dict[str, Any]]:
    # 优先读统一命名的新文件 bank_custom_<id>.json
    try:
        data = json.loads(_custom_file(subject).read_text("utf-8"))
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("questions"), list):
            return data["questions"]
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    # 兼容旧 physics 文件 bank_custom.json
    legacy = _legacy_custom_file(subject)
    if legacy:
        try:
            data = json.loads(legacy.read_text("utf-8"))
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
    # 兼容迁移：若旧 physics 文件 bank_custom.json 仍存在，写入新文件后删除旧文件
    legacy = _legacy_custom_file(subject)
    if legacy and legacy.is_file():
        try:
            legacy.unlink()
        except OSError:
            pass


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
    with _BANK_LOCK:
        _BANK.pop(subject, None)
    return {"imported": imported, "errors": errors}


def _parse_choices(raw: Any) -> list[str]:
    choices = raw.get("choices")
    if not isinstance(choices, list) or len(choices) < 2 or any(str(c).strip() == "" for c in choices):
        raise ValueError("choices 至少 2 个且非空")
    return [str(c).strip() for c in choices]


def _parse_indices(raw: Any, n: int) -> list[int]:
    """把 answer 解析为选项下标列表（支持 int / [int] / '0,2' / 'A,C'）。"""
    if isinstance(raw, int):
        return [raw]
    if isinstance(raw, list):
        out = []
        for x in raw:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                raise ValueError("answer 下标必须为整数")
        return out
    if isinstance(raw, str):
        out = []
        for part in re.split(r"[,，;；\s]+", raw.strip()):
            part = part.strip()
            if not part:
                continue
            if part.isalpha() and len(part) == 1:
                out.append(ord(part.upper()) - 65)
            else:
                try:
                    out.append(int(part))
                except ValueError:
                    raise ValueError("answer 下标非法")
        return out
    raise ValueError("answer 格式非法")


def _normalize_question(raw: Any, idx: int, used_ids: set[str], subject: str,
                        prefix: str = "", parent_stem: str = "") -> dict[str, Any]:
    """归一化单题（含 composite 递归子题）。prefix 用于复合题内部 id 命名空间。"""
    if not isinstance(raw, dict):
        raise ValueError("必须为对象")
    qtype = str(raw.get("type") or "").strip().lower()
    if not qtype:
        # 向后兼容：有 choices 且无 parts → 视为单选
        if isinstance(raw.get("choices"), list) and "parts" not in raw:
            qtype = "single"
        else:
            raise ValueError("缺少 type 字段")
    if qtype not in QUESTION_TYPES:
        raise ValueError(f"未知题型 type={qtype}")

    # composite 的题干为引导语，长度不限（可为空）；
    # 其余题型：顶层题干 >= 5 字，子题（parent_stem 非空）放宽为非空即可
    stem = str(raw.get("stem") or raw.get("question") or "").strip()
    if qtype != "composite" and len(stem) < (1 if parent_stem else 5):
        raise ValueError("题干过短" if not parent_stem else "子题题干不能为空")
    stem = stem or parent_stem

    raw_id = str(raw.get("id") or "").strip()
    if not raw_id:
        raw_id = f"custom-{idx}"
    raw_id = re.sub(r"[^0-9A-Za-z_\-]", "-", raw_id)[:60]
    if prefix:
        raw_id = f"{prefix}__{raw_id}"
    if raw_id in used_ids and raw_id.startswith("custom-"):
        raw_id = f"custom-{idx}-{raw_id}"

    item: dict[str, Any] = {
        "id": raw_id,
        "type": qtype,
        "subject": str(raw.get("subject") or subject).strip()[:20] or subject,
        "unit": str(raw.get("unit") or "未分类").strip()[:20] or "未分类",
        "chapter": str(raw.get("chapter") or "自选题").strip()[:30] or "自选题",
        "concept": str(raw.get("concept") or "其他").strip()[:40] or "其他",
        "difficulty": int(raw.get("difficulty") or 2),
        "stem": stem,
        "title": str(raw.get("title") or "").strip(),
        "explain": str(raw.get("explain") or "").strip(),
    }
    if not (1 <= item["difficulty"] <= 5):
        item["difficulty"] = 2

    if qtype == "single":
        choices = _parse_choices(raw)
        ans = _parse_indices(raw.get("answer"), len(choices))
        if len(ans) != 1:
            raise ValueError("单选 answer 必须为单个下标")
        if not (0 <= ans[0] < len(choices)):
            raise ValueError("answer 下标越界")
        item["choices"], item["answer"] = choices, ans[0]
    elif qtype == "multiple":
        choices = _parse_choices(raw)
        ans = _parse_indices(raw.get("answer"), len(choices))
        if not ans or any(not (0 <= a < len(choices)) for a in ans):
            raise ValueError("多选 answer 必须为合法下标列表（≥1）")
        item["choices"], item["answer"] = choices, sorted(set(ans))
    elif qtype == "fill":
        ans = raw.get("answer")
        if isinstance(ans, list):
            if not ans or any(str(a).strip() == "" for a in ans):
                raise ValueError("填空 answer 不可为空")
            item["answer"] = [str(a).strip() for a in ans]
        elif isinstance(ans, str) and ans.strip():
            item["answer"] = ans.strip()
        else:
            raise ValueError("填空 answer 必填（字符串或数组）")
        if isinstance(raw.get("choices"), list):
            item["choices"] = [str(c).strip() for c in raw["choices"]]
    elif qtype == "subjective":
        ans = str(raw.get("answer") or "").strip()
        if not ans:
            raise ValueError("主观题 answer 为参考答案，必填")
        item["answer"] = ans
        if isinstance(raw.get("choices"), list):
            item["choices"] = [str(c).strip() for c in raw["choices"]]
    elif qtype == "composite":
        parts = raw.get("parts")
        if not isinstance(parts, list) or not parts:
            raise ValueError("大小题 parts 必须为非空数组")
        item["parts"] = [
            _normalize_question(p, i + 1, used_ids, subject, prefix=raw_id,
                                parent_stem=stem)
            for i, p in enumerate(parts)
        ]
    return item


def _normalize_item(raw: Any, idx: int, used_ids: set[str], subject: str = "physics") -> dict[str, Any]:
    return _normalize_question(raw, idx, used_ids, subject)


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
        pub = _pub_item(item)
        pub["status"] = s
        out.append(pub)
    return out


def _pub_item(item: dict[str, Any]) -> dict[str, Any]:
    """对外题目（剥离 answer/explain，避免答案提前泄露）；复合题递归剥离子题。

    fill 题额外附带 blanks（空数）：前端需要知道渲染几个空，但拿不到答案本体。
    """
    pub = {k: v for k, v in item.items() if k not in _PRIVATE_FIELDS}
    if item.get("type") == "composite":
        pub["parts"] = [_pub_item(p) for p in item.get("parts", [])]
    elif item.get("type") == "fill":
        pub["blanks"] = len(item.get("answer") or []) if isinstance(item.get("answer"), list) else 1
    return pub


def _problem_content(item: dict[str, Any]) -> str:
    """错题本建档用的完整题面：composite 拼引导语+全部子题（递归、含选项），其余为题干+选项。"""
    if item.get("type") == "composite":
        lines = [str(item.get("stem") or "").strip()]
        for i, p in enumerate(item.get("parts", []), 1):
            lines.append(f"({i}) {_problem_content(p)}")
        return "\n".join(x for x in lines if x)[:2000]
    text = str(item.get("stem") or "")
    if isinstance(item.get("choices"), list) and item["choices"]:
        text += "\n" + "\n".join(
            f"{chr(65 + i)}. {c}" for i, c in enumerate(item["choices"][:8]))
    return text


def _problem_title(item: dict[str, Any]) -> str:
    """错题本标题兜底：题干过短/为空（composite 引导语可为空）时用首个子题题干。"""
    stem = str(item.get("stem") or "").strip()
    if len(stem) >= 5:
        return stem[:24]
    for p in item.get("parts") or []:
        sub = str(p.get("stem") or "").strip()
        if sub:
            return sub[:24]
    return "大小题"


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

    title = str(item.get("title") or "") or _problem_title(item)
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
        _problem_content(item),
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


def _grade_fill(user_raw: Any, correct_raw: Any) -> bool:
    """填空判分：逐空归一化（去首尾空白/标点、转小写）后按序比较。"""
    def _to_list(x: Any) -> list[str]:
        if isinstance(x, list):
            return [str(v) for v in x]
        return [str(x)]

    u, c = _to_list(user_raw), _to_list(correct_raw)
    if len(u) != len(c):
        return False
    return all(_norm_fill(a) == _norm_fill(b) for a, b in zip(u, c))


def _norm_fill(s: Any) -> str:
    return re.sub(r"[\s\.。，,、；;:：]+$", "", str(s).strip().lower())


def grade_item(item: dict[str, Any], user_raw: Any) -> dict[str, Any]:
    """递归判分。返回 {type, correct(bool|None), needs_review, answer, explain, parts?}。
    correct=None 表示主观待评阅（不参与对错、不自动入错题库）。"""
    t = item.get("type", "single")
    if t == "single":
        return {"type": t, "correct": _grade_answer(user_raw, item["answer"]),
                "answer": item["answer"], "explain": item.get("explain", "")}
    if t == "multiple":
        try:
            user_idx = set(_parse_indices(user_raw, len(item["choices"])))
        except ValueError:
            user_idx = set()
        return {"type": t, "correct": user_idx == set(item["answer"]),
                "answer": item["answer"], "explain": item.get("explain", "")}
    if t == "fill":
        return {"type": t, "correct": _grade_fill(user_raw, item["answer"]),
                "answer": item["answer"], "explain": item.get("explain", "")}
    if t == "subjective":
        return {"type": t, "correct": None, "needs_review": True,
                "answer": item["answer"], "explain": item.get("explain", "")}
    if t == "composite":
        parts_in = user_raw if isinstance(user_raw, list) else []
        results = [grade_item(p, parts_in[i] if i < len(parts_in) else None)
                   for i, p in enumerate(item.get("parts", []))]
        needs_review = any(r.get("needs_review") for r in results)
        auto = [r["correct"] for r in results if r["correct"] is not None]
        correct = None if needs_review else (all(auto) if auto else None)
        return {"type": t, "correct": correct, "needs_review": needs_review,
                "explain": item.get("explain", ""), "parts": results}
    # 兜底：无 type 当单选
    return {"type": "single", "correct": _grade_answer(user_raw, item.get("answer")),
            "answer": item.get("answer"), "explain": item.get("explain", "")}


def judge(qid: str, answer: Any, subject: str = "physics") -> dict[str, Any]:
    """判分。答错时自动建档入错题库。主观题标记待评阅（correct=None），不自动入错题库。"""
    bank = load_bank(subject)
    item = next((q for q in bank["questions"] if q["id"] == qid), None)
    if not item:
        # 学科不匹配时回退：全学科查找（qid 全局唯一）
        # C1e：迭代持 _BANK_LOCK，防并发 import_questions 重建 _BANK 时 RuntimeError
        with _BANK_LOCK:
            for s, bk in _BANK.items():
                item = next((q for q in bk["questions"] if q["id"] == qid), None)
                if item:
                    break
    if not item:
        raise ValueError("题目不存在")
    result = grade_item(item, answer)
    correct = result["correct"]
    # correct=None（主观待评阅）视为已作答，不计入错题
    cval = 1 if correct is None else (1 if correct else 0)
    problem_id = 0
    with DB_LOCK, db() as conn:
        conn.execute(
            "INSERT INTO bank_attempts(qid, correct, attempted_at) VALUES (?, ?, ?)",
            (qid, cval, now()),
        )
        if correct is False:
            problem_id = _ensure_problem(conn, item)
    resp = {
        "correct": correct,
        "needs_review": result.get("needs_review", False),
        "explain": result.get("explain", ""),
        "problem_id": problem_id,
    }
    if item.get("type") == "composite":
        resp["parts"] = result["parts"]
    else:
        resp["answer"] = item["answer"]
    return resp


def save_score_history(qid: str, subject: str, result: dict[str, Any]) -> int:
    """持久化一次 AI 评分结果（bank_scores）。返回新记录 id（无法落库时 0）。"""
    try:
        with DB_LOCK, db() as conn:
            cur = conn.execute(
                "INSERT INTO bank_scores(qid, subject, score, comment, against, mode, needs_review, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (str(qid)[:120], str(subject)[:20],
                 None if result.get("score") is None else int(result["score"]),
                 str(result.get("comment") or "")[:2000],
                 str(result.get("against") or "")[:2000],
                 str(result.get("mode") or "unrated")[:20],
                 1 if result.get("needs_review") else 0,
                 now()),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
    except Exception as exc:
        LOG.warning("评分历史落库失败: %s", exc)
        return 0


def recent_scores(qid: str, limit: int = 10) -> list[dict[str, Any]]:
    """按 qid 查询最近评分历史（时间倒序）。"""
    out: list[dict[str, Any]] = []
    try:
        for r in rows(
            "SELECT id, score, comment, against, mode, needs_review, created_at "
            "FROM bank_scores WHERE qid = ? ORDER BY id DESC LIMIT ?",
            (str(qid)[:120], int(limit)),
        ):
            out.append({
                "id": int(r["id"]),
                "score": r["score"],           # None → 未评分
                "comment": r["comment"],
                "against": r["against"],
                "mode": r["mode"],
                "needs_review": bool(r["needs_review"]),
                "created_at": r["created_at"],
            })
    except Exception as exc:
        LOG.warning("评分历史查询失败: %s", exc)
    return out