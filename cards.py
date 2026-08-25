"""概念闪卡 — 主动回忆（recall）的正向载体（Phase 1）。

定位：补「输入侧内化」缺口——把图谱概念变成可自检的记忆卡片（正面 cue、
背面 answer），复用 FSRS 调度做间隔复习。与 problems/reviews 完全解耦：
不参与题目掌握度统计，避免污染既有指标。

卡型 kind：
  - qa   问答卡（正面=问题/概念名，背面=自己作答后核对）
  - cloze 填空卡（正面=含空题干，背面=答案）
  - note 注解卡（正面=要点标题，背面=展开内容）

状态 status：active=启用（进入复习队列）/ draft=AI 草稿（未确认）/ disabled=停用
来源 source：manual=手填 / ai=AI 生成 / graph=由概念派生
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from config import LOG
from db import DB_LOCK, db, now, row, rows
from review import compute_review
import fsrs_bridge

_QA_SUFFIX = "请先合上资料，用自己的话解释或默写答案，再翻面对照。"


def _due_today() -> str:
    return date.today().isoformat()


def concept_lookup(concept_id: int) -> dict[str, Any] | None:
    """取概念名与详解（生成离线卡与展示用）。"""
    return row("SELECT id, name, explanation FROM concepts WHERE id = ?", (concept_id,))


def list_cards(subject: str, status: str = "") -> list[dict[str, Any]]:
    """列卡片（含概念名）。status 为空 → 全部。"""
    if status:
        items = rows(
            "SELECT c.*, k.name AS concept_name FROM cards c "
            "LEFT JOIN concepts k ON k.id = c.concept_id "
            "WHERE c.subject = ? AND c.status = ? ORDER BY c.id DESC", (subject, status))
    else:
        items = rows(
            "SELECT c.*, k.name AS concept_name FROM cards c "
            "LEFT JOIN concepts k ON k.id = c.concept_id "
            "WHERE c.subject = ? ORDER BY c.id DESC", (subject,))
    for it in items:
        it.pop("concept_id", None)
    return items


def due_cards(subject: str) -> list[dict[str, Any]]:
    """到期复习队列（status=active 且 due_date<=今天，按到期升序）。"""
    items = rows(
        "SELECT c.*, k.name AS concept_name FROM cards c "
        "LEFT JOIN concepts k ON k.id = c.concept_id "
        "WHERE c.subject = ? AND c.status = 'active' AND c.due_date != '' "
        "AND c.due_date <= ? ORDER BY c.due_date ASC",
        (subject, _due_today()))
    for it in items:
        it.pop("concept_id", None)
        # 逾期顺延展示：与题目复习一致，逾期久排在前面
        try:
            overdue = (date.today() - date.fromisoformat(it["due_date"])).days
        except (ValueError, TypeError):
            overdue = 0
        it["overdue_days"] = max(0, overdue)
    items.sort(key=lambda c: -c["overdue_days"])
    return items


def stats(subject: str) -> dict[str, int]:
    d = _due_today()
    with DB_LOCK, db() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM cards WHERE subject = ?", (subject,)).fetchone()["c"]
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM cards WHERE subject = ? AND status = 'active'", (subject,)).fetchone()["c"]
        due = conn.execute(
            "SELECT COUNT(*) AS c FROM cards WHERE subject = ? AND status = 'active' "
            "AND due_date != '' AND due_date <= ?", (subject, d)).fetchone()["c"]
        learned = conn.execute(
            "SELECT COUNT(*) AS c FROM cards WHERE subject = ? AND status='active' AND repetition >= 2",
            (subject,)).fetchone()["c"]
    return {"total": int(total), "active": int(active), "due": int(due), "learned": int(learned)}


def create_card(card_id: int | None, subject: str, concept_id: int, cue: str, answer: str,
                kind: str = "qa", source: str = "manual", status: str = "active") -> int:
    """新建（或覆盖同 id 的草稿）为一张启用状态卡片。返回 card id。"""
    cue = str(cue or "").strip()
    answer = str(answer or "").strip()
    if not cue:
        raise ValueError("卡片正面（cue）不能为空")
    kind = kind if kind in ("qa", "cloze", "note") else "qa"
    s = status if status in ("active", "draft", "disabled") else "active"
    with DB_LOCK, db() as conn:
        if card_id:
            conn.execute(
                "UPDATE cards SET subject = ?, concept_id = ?, kind = ?, cue = ?, answer = ?, "
                "status = ?, source = ?, due_date = ? WHERE id = ?",
                (subject, int(concept_id or 0), kind, cue, answer, s, source, _due_today(), card_id))
            return int(card_id)
        cur = conn.execute(
            "INSERT INTO cards(subject, concept_id, kind, cue, answer, status, source, created_at, due_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (subject, int(concept_id or 0), kind, cue, answer, s, source, now(), _due_today()))
        return int(cur.lastrowid)


def delete_card(card_id: int) -> bool:
    with DB_LOCK, db() as conn:
        cur = conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
    return cur.rowcount > 0


# ── 草稿生成（AI / 离线降级）─────────────────────────────

_OFFLINE_QA_TMPL = "用自己的话解释「{name}」{suffix}"
_OFFLINE_CLOZE_TMPL = "填空：『{name}』指的是 ______。{suffix}"


def offline_drafts(concept_id: int) -> list[dict[str, str]]:
    """无 AI / AI 失败时：由概念详解生成「定义自检」卡。"""
    c = concept_lookup(concept_id)
    name = c["name"] if c else f"概念#{concept_id}"
    base = (c or {}).get("explanation") or name
    return [
        {"kind": "qa", "cue": _OFFLINE_QA_TMPL.format(name=name, suffix=_QA_SUFFIX), "answer": base},
        {"kind": "cloze", "cue": _OFFLINE_CLOZE_TMPL.format(name=name, suffix=_QA_SUFFIX), "answer": name},
    ]


_CARD_SCHEMA = {
    "cards": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "cue": {"type": "string", "min_length": 1, "required": True},
                "answer": {"type": "string", "min_length": 1, "required": True},
                "kind": {"type": "string", "enum": ["qa", "cloze", "note"], "required": True},
            },
        },
        "required": True,
    },
}


def generate_drafts(subject: str, concept_id: int, use_ai: bool = True) -> list[dict[str, str]]:
    """生成卡片草稿（不落库）。AI 可用且 use_ai → AI 出 QA/填空卡；否则离线定义自检卡。
    返回 [{cue, answer, kind}]，供前端核对后确认。
    """
    c = concept_lookup(concept_id)
    name = c["name"] if c else None
    if use_ai:
        try:
            from ai import ai_configured, call_ai
            from validate import validate_object, SchemaError
            if ai_configured():
                base = (c or {}).get("explanation") or ""
                explanation = f"\n概念详解：{base}" if base else ""
                prompt = (
                    f"你是{subject}学科助教。针对概念「{name}」{explanation}\n"
                    "制作 2 张适合主动回忆（先合上资料在脑中作答，再翻面对照）的闪卡，"
                    "只返回严格 JSON：{\"cards\":[{\"cue\":\"正面（问题/题干/含空提示）\","
                    "\"answer\":\"背面（标准答案/要点，简洁准确）\",\"kind\":\"qa 或 cloze\"}]}。"
                    "不要 Markdown，不要题外话。"
                )
                raw = call_ai(
                    [{"role": "user", "content": prompt}],
                    max_tokens=1200, tier="heavy", route="cards",
                )
                data = validate_object(raw, _CARD_SCHEMA)
                out: list[dict[str, str]] = []
                for d in data.get("cards", [])[:4]:
                    out.append({
                        "cue": str(d["cue"]).strip()[:300],
                        "answer": str(d["answer"]).strip()[:2000],
                        "kind": d["kind"] if d["kind"] in ("qa", "cloze", "note") else "qa",
                    })
                if out:
                    return out
                LOG.warning("AI 卡片生成返回空，回退离线草稿")
        except (SchemaError, ValueError) as exc:
            LOG.warning("AI 卡片生成校验失败，回退离线草稿: %s", exc)
        except Exception as exc:
            LOG.warning("AI 卡片生成失败，回退离线草稿: %s", exc)
    if not name:
        raise ValueError("概念不存在")
    return offline_drafts(concept_id)


# ── 复习（FSRS 调度）────────────────────────────────────

def review_card(card_id: int, rating: int) -> dict[str, Any]:
    """按评分更新卡片调度状态并写评分日志。返回下次到期信息。
    rating: 1 忘 / 2 模糊 / 3 基本对 / 4 掌握。
    """
    card = row("SELECT * FROM cards WHERE id = ?", (card_id,))
    if not card:
        raise ValueError("卡片不存在")
    rating = max(1, min(4, int(rating or 2)))
    prev = card["interval_days"] or 1
    result = compute_review(rating, prev, float(card["ease_factor"] or 2.5), int(card["repetition"] or 0))
    # FSRS 可用 → 使用 FSRS 间隔；否则 SM-2 间隔
    # C2b：只算一次并复用（原实现双算 compute_fsrs_review，参数完全相同）
    fs = None
    if fsrs_bridge.fsrs_available():
        try:
            fs = fsrs_bridge.compute_fsrs_review(
                rating, prev,
                int(card["state"] or 0),
                float(card["stability"] or 0),
                float(card["difficulty"] or 0),
            )
            result.interval_days = max(1, fs.scheduled_days)
        except Exception as exc:
            LOG.warning("卡片 FSRS 调度异常，回退 SM-2: %s", exc)
    # 逾期顺延（与题目复习一致）：长逾期降级为新学
    try:
        overdue = (date.today() - date.fromisoformat(card["due_date"])).days
    except (ValueError, TypeError):
        overdue = 0
    if overdue >= 21:
        result.repetition = 0
        result.ease_factor = min(result.ease_factor, 2.5)
        result.interval_days = 1
    elif overdue >= 5:
        result.interval_days = max(1, result.interval_days // 2)
    next_due = (date.today() + timedelta(days=result.interval_days)).isoformat()
    fs_state, fs_stability, fs_difficulty = 0, 0.0, 0.0
    if fs is not None and overdue < 21:
        fs_state, fs_stability, fs_difficulty = fs.state, fs.stability, fs.difficulty
    with DB_LOCK, db() as conn:
        conn.execute("INSERT INTO card_reviews(card_id, due_date, interval_days, rating, created_at) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (card_id, next_due, result.interval_days, rating, now()))
        conn.execute(
            "UPDATE cards SET due_date = ?, interval_days = ?, ease_factor = ?, repetition = ?, "
            "state = ?, stability = ?, difficulty = ?, last_review = ? WHERE id = ?",
            (next_due, result.interval_days, result.ease_factor, result.repetition,
             fs_state, fs_stability, fs_difficulty, date.today().isoformat(), card_id))
    # Phase 2：卡片评分实时回写图谱掌握度（双驱动）
    try:
        import graph
        graph.update_progress(card["subject"], force=True)
    except Exception as exc:
        LOG.debug("卡片评分后图谱掌握度重算失败（可忽略）: %s", exc)
    return {"card_id": card_id, "next_due": next_due, "interval_days": result.interval_days,
            "mastery": result.mastery}