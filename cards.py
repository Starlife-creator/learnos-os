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
import graph
import trash

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
    """到期复习队列（status=active 且 due_date<=今天，按到期升序）。

    B6 P2-1：每张卡附 `familiarity`（4 档键 hazy/shaky/familiar/solid），前端 i18n
    翻成本地化词显示。FSRS 未启用时所有卡落到 hazy（语义：还没记录）。
    """
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
        # B6 P2-1：调 fsrs_bridge.retrievability 算 R，再 familiarity_label 翻 4 档
        try:
            R = fsrs_bridge.retrievability(
                prev_interval=int(it.get("interval_days") or 0),
                state=int(it.get("state") or 0),
                stability=float(it.get("stability") or 0.0),
                difficulty=float(it.get("difficulty") or 0.0),
                last_review=str(it.get("last_review") or ""),
                subject=subject,
            )
            it["retrievability"] = R
            it["familiarity"] = fsrs_bridge.familiarity_label(R)
        except Exception as exc:
            LOG.debug("卡片熟悉度估算失败（可忽略）: %s", exc)
            it["retrievability"] = 0.0
            it["familiarity"] = fsrs_bridge.FAM_HAZY
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


# ── C2 语义契约：先查后写 + 泄题检查 ──────────────────────

def _norm_cue(text: str) -> str:
    """cue 归一化：casefold（Unicode 全折叠，SQLite NOCASE 只折 ASCII）+ 空白折叠。"""
    return " ".join(str(text or "").split()).casefold()


def _norm_dense(text: str) -> str:
    """泄题探测用归一化：casefold + 去除所有空白（子串探测对空白差异鲁棒）。"""
    return "".join(str(text or "").split()).casefold()


def find_duplicate_card(subject: str, cue: str, exclude_id: int | None = None) -> dict[str, Any] | None:
    """C2 先查后写：同学科下 cue 归一化相同的已有卡（含 draft，防草稿重复确认）。

    返回重复卡行（含 id/cue/status），无重复返回 None。归一化在应用层做：
    SQLite NOCASE 排序规则只折叠 ASCII，中文/全角场景必须逐行比对。
    """
    key = _norm_cue(cue)
    if not key:
        return None
    for c in rows("SELECT id, cue, kind, status FROM cards WHERE subject = ?", (subject,)):
        if c["id"] != exclude_id and _norm_cue(c["cue"]) == key:
            return c
    return None


def leaks_answer(cue: str, answer: str) -> bool:
    """C2 泄题检查：正面（cue）含完整答案（answer）即泄题。

    归一化（casefold + 去全部空白）子串探测；答案过短（<4 字符，如符号/年份）
    不判泄题，避免「答案=F」误伤所有正面。
    """
    a = _norm_dense(answer)
    return len(a) >= 4 and a in _norm_dense(cue)


def create_card(card_id: int | None, subject: str, concept_id: int, cue: str, answer: str,
                kind: str = "qa", source: str = "manual", status: str = "active") -> int:
    """新建（或覆盖同 id 的草稿）为一张启用状态卡片。返回 card id。

    C2 语义契约：①先查后写——同学科下 cue 归一化重复（大小写/空白差异视为相同）
    拒绝并提示编辑原卡；②泄题检查——正面含完整答案拒绝，要求改写正面。
    """
    cue = str(cue or "").strip()
    answer = str(answer or "").strip()
    if not cue:
        raise ValueError("卡片正面（cue）不能为空")
    if leaks_answer(cue, answer):
        raise ValueError("正面包含完整答案（泄题），请改写正面或拆短背面")
    dup = find_duplicate_card(subject, cue, exclude_id=card_id)
    if dup:
        raise ValueError(f"已存在相同正面的卡片 #{dup['id']}（{dup['status']}），请编辑原卡而非重复新建")
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
    """D3：删除先入回收站（卡片 + 级联评分日志全量快照），保留期内可原样恢复。"""
    with DB_LOCK, db() as conn:
        if not conn.execute("SELECT 1 FROM cards WHERE id = ?", (card_id,)).fetchone():
            return False
        trash.snapshot(conn, "card", card_id, [
            ("cards", "SELECT * FROM cards WHERE id = ?", (card_id,)),
            ("card_reviews", "SELECT * FROM card_reviews WHERE card_id = ?", (card_id,)),
        ])
        conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))  # 评分日志随 FK 级联
    return True


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
                    max_tokens=1200, tier="heavy", route="cards", json_mode=True,
                )
                data = validate_object(raw, _CARD_SCHEMA)
                out: list[dict[str, str]] = []
                for d in data.get("cards", [])[:4]:
                    item = {
                        "cue": str(d["cue"]).strip()[:300],
                        "answer": str(d["answer"]).strip()[:2000],
                        "kind": d["kind"] if d["kind"] in ("qa", "cloze", "note") else "qa",
                    }
                    # C2 语义契约（草稿回炉）：泄题（正面含完整答案）或与已有卡
                    # 重复的草稿直接丢弃；前端只看到干净草稿，全滤光则回退离线。
                    if leaks_answer(item["cue"], item["answer"]):
                        LOG.warning("AI 草稿泄题（正面含完整答案），已回炉丢弃: %.60s", item["cue"])
                        continue
                    if find_duplicate_card(subject, item["cue"]):
                        LOG.warning("AI 草稿与已有卡重复，已丢弃: %.60s", item["cue"])
                        continue
                    out.append(item)
                if out:
                    return out
                LOG.warning("AI 卡片生成返回空（或全部泄题/重复），回退离线草稿")
        except (SchemaError, ValueError) as exc:
            LOG.warning("AI 卡片生成校验失败，回退离线草稿: %s", exc)
        except Exception as exc:
            LOG.warning("AI 卡片生成失败，回退离线草稿: %s", exc)
    if not name:
        raise ValueError("概念不存在")
    return offline_drafts(concept_id)


def generate_batch_drafts(subject: str, concept_ids: list[int],
                          use_ai: bool = True) -> dict[str, Any]:
    """C1 制卡 Pipeline 分层：按概念里程碑清单逐块出卡（而非整本硬塞）。

    输入是「课纲步骤」产出的概念 id 清单（资料导入向导的课纲提取 / 学习路径的
    薄弱概念），逐概念独立调用 generate_drafts（单概念小上下文，质量更高且
    失败互不影响）；单概念失败仅跳过并记入 failed，不中断整批。
    AI 层重试/降级由 generate_drafts 内部约定负责（call_ai 自带重试 + 离线模板降级）。
    返回 {results: [{concept_id, concept_name, drafts}], failed: [{concept_id, error}]}。
    """
    results: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    seen: set[int] = set()
    for cid in concept_ids[:60]:  # 上限 60 概念防失控账单
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            continue
        if cid in seen:
            continue
        seen.add(cid)
        try:
            drafts = generate_drafts(subject, cid, use_ai=use_ai)
            name = (concept_lookup(cid) or {}).get("name") or f"概念#{cid}"
            if drafts:
                results.append({"concept_id": cid, "concept_name": name, "drafts": drafts})
            else:
                failed.append({"concept_id": cid, "error": "未产出草稿"})
        except ValueError as exc:
            failed.append({"concept_id": cid, "error": str(exc)})
        except Exception as exc:  # 单概念异常不拖垮整批
            LOG.warning("批量制卡：概念#%d 失败已跳过: %s", cid, exc)
            failed.append({"concept_id": cid, "error": str(exc)})
    return {"results": results, "failed": failed}


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
    # D1：评分前完整记忆态快照（撤销=原子恢复；审计=可重放）
    prev_snap = {
        "state": int(card["state"] or 0),
        "stability": float(card["stability"] or 0.0),
        "difficulty": float(card["difficulty"] or 0.0),
        "due": card["due_date"] or "",
        "interval": int(card["interval_days"] or 1),
        "repetition": int(card["repetition"] or 0),
        "ease": float(card["ease_factor"] or 2.5),
        "last_review": card["last_review"] or "",
    }
    with DB_LOCK, db() as conn:
        # D1+F2：评分日志写「不可变事实」——prev 快照 + cur FSRS 三态 + 参数指纹
        # （cur_due/cur_interval 复用既有 due_date/interval_days 列）
        conn.execute(
            "INSERT INTO card_reviews(card_id, due_date, interval_days, rating, created_at, "
            "prev_state, prev_stability, prev_difficulty, prev_due, prev_interval, "
            "prev_repetition, prev_ease, prev_last_review, "
            "cur_state, cur_stability, cur_difficulty, fsrs_params_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (card_id, next_due, result.interval_days, rating, now(),
             prev_snap["state"], prev_snap["stability"], prev_snap["difficulty"],
             prev_snap["due"], prev_snap["interval"], prev_snap["repetition"],
             prev_snap["ease"], prev_snap["last_review"],
             fs_state, fs_stability, fs_difficulty,
             fsrs_bridge.current_params_hash()))
        conn.execute(
            "UPDATE cards SET due_date = ?, interval_days = ?, ease_factor = ?, repetition = ?, "
            "state = ?, stability = ?, difficulty = ?, last_review = ? WHERE id = ?",
            (next_due, result.interval_days, result.ease_factor, result.repetition,
             fs_state, fs_stability, fs_difficulty, date.today().isoformat(), card_id))
    # Phase 2：卡片评分实时回写图谱掌握度（双驱动）
    try:
        graph.update_progress(card["subject"], force=True, entry_point="card",
                              evidence=f"闪卡#{card_id} 评分{rating}")
    except Exception as exc:
        LOG.debug("卡片评分后图谱掌握度重算失败（可忽略）: %s", exc)
    return {"card_id": card_id, "next_due": next_due, "interval_days": result.interval_days,
            "mastery": result.mastery}


def undo_review(card_id: int) -> dict[str, Any]:
    """D2 撤销最近一次评分：原子恢复 prev 全字段到 cards，并作废末行日志（undone=1）。

    只撤最近一行未作废日志——连撤按行数自然递减（不越界：无行可撤报错）；
    旧版本数据行（prev_due 为空串，无快照语义）显式拒绝；
    撤销后触发掌握度重算（与评分路径对称）。
    """
    with DB_LOCK, db() as conn:
        last = conn.execute(
            "SELECT * FROM card_reviews WHERE card_id = ? AND undone = 0 "
            "ORDER BY id DESC LIMIT 1", (card_id,)).fetchone()
        if not last:
            raise ValueError("没有可撤销的评分记录")
        if not last["prev_due"]:
            raise ValueError("该评分记录无快照（旧版本数据），不可撤销")
        conn.execute(
            "UPDATE cards SET due_date = ?, interval_days = ?, repetition = ?, ease_factor = ?, "
            "state = ?, stability = ?, difficulty = ?, last_review = ? WHERE id = ?",
            (last["prev_due"], last["prev_interval"], last["prev_repetition"],
             last["prev_ease"], last["prev_state"], last["prev_stability"],
             last["prev_difficulty"], last["prev_last_review"], card_id))
        conn.execute("UPDATE card_reviews SET undone = 1 WHERE id = ?", (last["id"],))
    card = row("SELECT subject FROM cards WHERE id = ?", (card_id,))
    if card:
        try:
            graph.update_progress(card["subject"], force=True, entry_point="card_undo",
                                  evidence=f"撤销闪卡#{card_id}评分（评分{last['rating']}）")
        except Exception as exc:
            LOG.debug("撤销评分后图谱掌握度重算失败（可忽略）: %s", exc)
    return {"card_id": card_id, "restored_due": last["prev_due"],
            "undone_review_id": last["id"]}