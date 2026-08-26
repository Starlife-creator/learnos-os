"""AI 口试模块：苏格拉底引擎（F1）。

校准级别状态机（level 1-3）× 五阶段引导（核心概念→前提→反例→边界→检验）：
- 回答扎实（level 3）→ 推进到下一阶段；
- 回答薄弱（level 1-2）→ 同阶段加深引导（更具体的追问模板）。
AI 可用时由模型出问（先内心评估再作答），离线时全本地模板降级。
提示人格按学科感知（physics 默认保留原措辞，chem/math/未知回落中性），无 schema 变更。
口试结束自动回写学习者画像（薄弱点），并可生成复习卡草稿（R3 由前端确认后落库）。
"""
from __future__ import annotations

import json
import re
from typing import Any

from config import LOG
from db import db, now, row, DB_LOCK
from ai import call_ai, _subject_profile, _resolve_subject
import profile

_STATE_KEY = "__oral_state__"
_MAX_TURNS = 5

_GUIDE_STAGES = ["concept", "premise", "counterexample", "extreme", "verify"]

_CONDITION_WORDS = ("条件", "前提", "假设", "当", "只要", "仅当", "要求", "满足")
_SYMBOL_RE = re.compile(
    r"[A-Za-z]\w*(?:\s*[=+\-*/^]|\s*[（(]|\s+[\d.]+)|[∑∫∂∇√πλμρθωΩ]|F\s*="
)
_END_RE = re.compile(r"^【口试结束】", re.MULTILINE)

# A5 Feynman 口述反转：向新手讲解 → 对照标准解析找漏点 → 自评表
_FEYNMAN_STEPS = [
    "step1_explain",
    "step2_review",
    "step3_selfcheck",
]


def start_feynman(problem: dict[str, Any]) -> tuple[int, str]:
    """A5：对错题启动 Feynman 口述反转会话（复用 F1 引导框架）。"""
    title = problem.get("title", "")
    content = problem.get("content", "")
    topic = problem.get("topic", "")
    sbj = _resolve_subject(problem.get("subject", ""), topic) or problem.get("subject", "")
    p = _subject_profile(sbj)
    question = (
        f"这是一道错题：{title}。\n题目：{content}\n"
        f"现在请把自己当作老师：{p['feynman_novice']}"
        "讲完直接发送，我会帮你对照标准解析找漏点。"
    )
    try:
        question = call_ai([
            {"role": "system", "content": (
                "你是 Feynman 学习法教练。围绕一道错题展开三步口述反转："
                "①向新手讲解核心概念（禁公式）；②对照标准解析找漏点；③生成自评表。"
                + _profile_context()
                + f"主题：{topic or title}。第一步请只提一个问题：让学生用大白话讲解。"
            )},
            {"role": "user", "content": f"题目：{content}\n标题：{title}"},
        ], max_tokens=200, route="oral")
    except Exception as exc:
        LOG.warning("Feynman 首问 AI 失败，使用内置问题: %s", exc)

    transcript = [{"role": "assistant", "content": question}]
    _write_state(transcript, 0, 1)
    with DB_LOCK, db() as conn:
        cursor = conn.execute(
            "INSERT INTO oral_sessions(topic, transcript, mode, problem_id, created_at) VALUES (?, ?, 'feynman', ?, ?)",
            (f"Feynman：{title}", json.dumps(transcript, ensure_ascii=False), int(problem.get("id", 0)), now()),
        )
        return int(cursor.lastrowid), question


def _feynman_followup(transcript: list[dict[str, str]], turn: int) -> str:
    """Feynman 第二步：对照标准解析找漏点（AI 或本地模板）。"""
    try:
        messages = [
            {"role": "system", "content": (
                "你是 Feynman 学习法教练。学生刚完成概念讲解。"
                "现在要求他对照标准解析自查。只输出要求，不要替他总结。"
            )},
            {"role": "user", "content": "讲解记录：" + json.dumps(transcript[-4:], ensure_ascii=False)},
        ]
        return call_ai(messages, max_tokens=220, tier="heavy", route="oral")
    except Exception as exc:
        LOG.warning("Feynman 对照引导失败，使用内置: %s", exc)
        return (
            "现在对照标准解析（可在详情页查看第③级提示）。请逐条自查刚才的讲解，"
            "按三行输出：\n漏点：…\n讲错：…\n讲清：…\n"
            "越具体越好，例如『漏点：没有说清适用前提』。"
        )


def _self_review_draft(transcript: list[dict[str, str]], problem_content: str) -> dict[str, list[str]]:
    """A5 自评表草稿：AI 从讲解与自查提取三列表，失败用本地规则。"""
    try:
        messages = [
            {"role": "system", "content": (
                "你是 Feynman 学习法教练。基于学生的讲解与自查，生成结构化自评表。"
                '只输出 JSON：{"gaps": ["漏掉的要点", ...], "wrong": ["讲错的地方", ...], "clear": ["讲清楚的地方", ...]}。'
                "漏点优先从『标准解析要点』对比得出，最多各 3 条。"
            )},
            {"role": "user", "content": (
                f"标准解析要点：{problem_content}\n对话记录：" + json.dumps(transcript[-6:], ensure_ascii=False)
            )},
        ]
        raw = call_ai(messages, max_tokens=300, tier="heavy", route="oral")
        data = json.loads(raw)
        return {
            "gaps": [str(x) for x in data.get("gaps", [])][:3],
            "wrong": [str(x) for x in data.get("wrong", [])][:3],
            "clear": [str(x) for x in data.get("clear", [])][:3],
        }
    except Exception as exc:
        LOG.warning("自评表 AI 生成失败，使用本地模板: %s", exc)
        return _local_self_review(transcript)


def _local_self_review(transcript: list[dict[str, str]]) -> dict[str, list[str]]:
    user_msgs = [m["content"] for m in transcript if m["role"] == "user"]
    gaps, wrong, clear = [], [], []
    for line in user_msgs:
        for name, bucket in (("漏点", gaps), ("讲错", wrong), ("讲清", clear)):
            for part in line.split("；"):
                if part.startswith(name + "："):
                    bucket.append(part[len(name) + 1:].strip())
                elif part.startswith(name + ":"):
                    bucket.append(part[len(name) + 1:].strip())
    if not gaps and not wrong:
        gaps.append("未能对照标准解析逐条自查，建议重新讲解")
    return {"gaps": gaps[:3], "wrong": wrong[:3], "clear": clear[:3] or ["概念口述完整"]}


def _read_state(transcript: list[dict[str, str]]) -> tuple[int, int]:
    for item in transcript:
        if item.get("role") == "system" and item.get("content", "").startswith(_STATE_KEY):
            try:
                s = json.loads(item["content"][len(_STATE_KEY):])
                return int(s.get("stage", 0)), int(s.get("level", 1))
            except (json.JSONDecodeError, TypeError, ValueError):
                return 0, 1
    return 0, 1


def _write_state(transcript: list[dict[str, str]], stage: int, level: int) -> None:
    state = _STATE_KEY + json.dumps({"stage": stage, "level": level}, ensure_ascii=False)
    for item in transcript:
        if item.get("role") == "system" and item.get("content", "").startswith(_STATE_KEY):
            item["content"] = state
            return
    transcript.insert(0, {"role": "system", "content": state})


def _assess(answer: str) -> int:
    """本地校准：按内容深度给出掌握级别 1-3（零依赖启发式）。"""
    score = 0
    if len(answer) >= 40:
        score += 1
    if _SYMBOL_RE.search(answer):
        score += 1
    if any(w in answer for w in _CONDITION_WORDS):
        score += 1
    return min(3, 1 + score)


def _next_stage(current: int, level: int) -> tuple[int, int]:
    if level >= 3 and current < len(_GUIDE_STAGES) - 1:
        return current + 1, 1
    return current, min(3, level)


def _profile_context() -> str:
    """学习者画像注入（Tutor-GPT 式心智模型）：失败静默，不影响口试主流程。"""
    try:
        from profile import snapshot
        line = snapshot()
    except Exception as exc:
        LOG.debug("口试画像注入失败（可忽略）: %s", exc)
        return ""
    return (f"{line}\n"
            "请基于以上档案动态调整教学策略：优先往学生的薄弱知识点方向追问，"
            "针对其高频错因设计检查点；回答质量判断参照其历史掌握水平。")


def start_oral(topic: str, subject: str = "") -> tuple[int, str]:
    sbj = _resolve_subject(subject, topic) or subject
    p = _subject_profile(sbj)
    state = "concept"
    question = p["stage_prompts"][state].format(topic=topic)
    try:
        question = call_ai([
            {"role": "system", "content": (
                p["oral_teacher"]
                + _profile_context()  # 画像日内稳定，前置以命中前缀缓存
                + f"学生刚开始学习「{topic}」。一次只问一个简洁的、关于核心概念的问题，不给答案。"
            )},
            {"role": "user", "content": f'围绕「{topic}」提出第一个概念理解问题。'},
        ], max_tokens=180, route="oral")
    except Exception as exc:
        LOG.warning("口试 AI 调用失败，使用内置问题: %s", exc)

    transcript = [{"role": "assistant", "content": question}]
    _write_state(transcript, 0, 1)
    with DB_LOCK, db() as conn:
        cursor = conn.execute(
            "INSERT INTO oral_sessions(topic, transcript, created_at) VALUES (?, ?, ?)",
            (topic, json.dumps(transcript, ensure_ascii=False), now()),
        )
        return int(cursor.lastrowid), question


def continue_oral(session: dict[str, Any], answer: str, subject: str = "") -> str:
    transcript = json.loads(session["transcript"])
    transcript.append({"role": "user", "content": answer})
    turn = sum(1 for item in transcript if item["role"] == "user")
    if session.get("mode") == "feynman":
        return _continue_feynman(session, transcript, answer, turn, subject)
    stage_idx, level = _read_state(transcript)
    stage = _GUIDE_STAGES[stage_idx]
    topic = session["topic"]
    sbj = _resolve_subject(subject, topic) or subject
    p = _subject_profile(sbj)

    if turn >= _MAX_TURNS:
        reply = _summary(transcript, topic, sbj)
    else:
        level = _assess(answer)
        stage_idx, level = _next_stage(stage_idx, level)
        stage = _GUIDE_STAGES[stage_idx]
        try:
            reply = _ai_followup(transcript, topic, stage, level, turn, sbj)
        except Exception as exc:
            LOG.warning("口试 AI 追问失败，使用本地模板: %s", exc)
            if level >= 3:
                reply = p["stage_prompts"][stage].format(topic=topic)
            else:
                reply = p["deeper_prompts"][stage].format(topic=topic)

    transcript.append({"role": "assistant", "content": reply})
    _write_state(transcript, stage_idx, level)
    status = "finished" if _END_RE.search(reply) or turn >= _MAX_TURNS else "active"
    with DB_LOCK, db() as conn:
        conn.execute(
            "UPDATE oral_sessions SET transcript = ?, status = ? WHERE id = ?",
            (json.dumps(transcript, ensure_ascii=False), status, session["id"]),
        )
    if status == "finished":
        _write_back_profile(transcript, topic, sbj)
    return reply


def _continue_feynman(session: dict[str, Any], transcript: list[dict[str, str]],
                      answer: str, turn: int, subject: str = "") -> str:
    """A5 Feynman 流程：讲解 → 对照自查 → 结束（自评表草稿经独立端点获取）。"""
    if turn >= 2:
        reply = (
            "【口试结束】讲解与自查完成。已生成自评表草稿，"
            "请确认「漏点 / 讲错 / 讲清」三列并保存——保存后漏点会标记到本题的复习队列，"
            "下次复习优先重考。"
        )
        status = "finished"
    else:
        reply = _feynman_followup(transcript, turn)
        status = "active"
    transcript.append({"role": "assistant", "content": reply})
    _write_state(transcript, turn, 1)
    with DB_LOCK, db() as conn:
        conn.execute(
            "UPDATE oral_sessions SET transcript = ?, status = ? WHERE id = ?",
            (json.dumps(transcript, ensure_ascii=False), status, session["id"]),
        )
    return reply


def feynman_self_review(session: dict[str, Any]) -> dict[str, Any]:
    """A5 自评表：已保存的直接返回；未保存的生成草稿（R3 不落库）。"""
    if session.get("self_review"):
        try:
            return {"draft": None, "saved": json.loads(session["self_review"])}
        except json.JSONDecodeError:
            pass
    problem = None
    if session.get("problem_id"):
        problem = row("SELECT content, title, topic FROM problems WHERE id = ?", (session["problem_id"],))
    problem_text = f"{problem['title']}：{problem['content']}" if problem else session.get("topic", "")
    draft = _self_review_draft(json.loads(session["transcript"]), problem_text)
    return {"draft": draft, "saved": None}


def save_feynman_self_review(session_id: int, values: dict[str, Any]) -> bool:
    """A5：用户确认后的自评表落库（R3 确认制）。"""
    clean = {
        "gaps": [str(x).strip() for x in values.get("gaps", []) if str(x).strip()][:3],
        "wrong": [str(x).strip() for x in values.get("wrong", []) if str(x).strip()][:3],
        "clear": [str(x).strip() for x in values.get("clear", []) if str(x).strip()][:3],
    }
    if not (clean["gaps"] or clean["wrong"] or clean["clear"]):
        return False
    with DB_LOCK, db() as conn:
        cur = conn.execute(
            "UPDATE oral_sessions SET self_review = ?, status = 'finished' WHERE id = ?",
            (json.dumps(clean, ensure_ascii=False), session_id),
        )
        return cur.rowcount > 0


def _ai_followup(transcript: list[dict[str, str]], topic: str, stage: str, level: int, turn: int, subject: str = "") -> str:
    """AI 引导追问：先内心评估，再输出「一句诊断 + 一个追问」，不给答案。"""
    p = _subject_profile(subject)
    user_msgs = [m for m in transcript if m["role"] == "user"]
    last_answer = user_msgs[-1]["content"] if user_msgs else ""
    remaining = _MAX_TURNS - turn
    instruction = (
        f"学生正在学习「{topic}」，当前引导阶段：{p['stage_labels'][stage]}，"
        f"本轮回答质量级别：{level}/3（1=薄弱 3=扎实）。"
        "先在内心评估学生回答最大的缺陷，然后只输出两行："
        "① 一句话诊断（指出最需修正或深化之处）；② 一个针对性的追问。不要给出完整答案。"
        f"剩余轮次：{remaining}。"
    )
    if level <= 1 and turn >= 2:
        # handoff 规则（教学研究：反复追问不点破会变成折磨）：同一薄弱点
        # 连续多轮回答薄弱时，先用一句通俗讲解点破关键，再用更小的验证性
        # 追问确认理解，避免无限提问循环。
        instruction += (
            "该生在此处已连续多轮回答薄弱：本轮先给一句简短通俗的讲解直接点破"
            "最关键的误区或缺失前提（不超过两句话），再提一个更小的验证性追问"
            "确认他是否真正理解。"
        )
    messages = [{"role": "system", "content":
                 p["oral_teacher"]
                 + _profile_context()  # 稳定前缀更长，命中缓存
                 + instruction}]
    messages.extend(transcript[-6:])
    return call_ai(messages, max_tokens=300, tier="heavy", route="oral")


def _summary(transcript: list[dict[str, str]], topic: str, subject: str = "") -> str:
    p = _subject_profile(subject)
    try:
        messages = [
            {"role": "system", "content": (
                p["oral_teacher_summary"]
                + "以【口试结束】开头。简短评价：指出一个掌握点和两个薄弱点（尽量具体，如『未说明XX的适用条件』），"
                "再给出 3 天内可执行的复习建议。不要问新问题。"
            )},
            {"role": "user", "content": "以下是本场口试的完整对话记录：" + json.dumps(transcript[-8:], ensure_ascii=False)},
        ]
        return call_ai(messages, max_tokens=400, tier="heavy", route="oral")
    except Exception as exc:
        LOG.warning("口试总结 AI 调用失败，使用本地总结: %s", exc)
        return (
            f"【口试结束】你已经完成 {_MAX_TURNS} 轮回答。"
            f"请回看关于「{topic}」的回答：哪些地方没有说明适用条件、哪些推导缺少依据。"
            "建议选择其中一个薄弱点，在明天重新口述一遍，并对照教材检查。"
        )


def _detect_weak_points(transcript: list[dict[str, str]], subject: str = "") -> list[str]:
    p = _subject_profile(subject)
    texts = [m["content"] for m in transcript if m["role"] == "user"]
    found: list[str] = []
    for pat, label in p["weak_patterns"]:
        if any(pat.search(t) for t in texts):
            found.append(label)
    return found or ["概念表述不够严谨"]


def _write_back_profile(transcript: list[dict[str, str]], topic: str, subject: str = "") -> None:
    """F1：会话结束回写学习者画像（仅本地，薄弱点追加到备注，不覆盖用户原备注）。"""
    try:
        weak = "、".join(_detect_weak_points(transcript, subject))
        entry = f"[口试 {topic}] 薄弱点：{weak}"
        cur = row("SELECT value FROM learner_profile WHERE key = 'note'")
        merged = entry if not cur or not cur["value"] else f"{cur['value']} | {entry}"
        profile.update({"note": merged[:500]})
    except Exception as exc:
        LOG.warning("口试画像回写失败: %s", exc)


def draft_oral_card(session: dict[str, Any]) -> dict[str, str]:
    """F1 流水线：口试 → 复习卡草稿（R3 不落库，前端确认后创建）。"""
    transcript = json.loads(session["transcript"])
    topic = session["topic"]
    sbj = _resolve_subject(session.get("subject", ""), topic) or session.get("subject", "")
    if not sbj and session.get("problem_id"):
        prob = row("SELECT subject FROM problems WHERE id = ?", (int(session["problem_id"]),))
        if prob:
            sbj = _resolve_subject(prob.get("subject", "")) or prob.get("subject", "")
    p = _subject_profile(sbj)
    user_answers = [m["content"] for m in transcript if m["role"] == "user"]
    weak = "、".join(_detect_weak_points(transcript, sbj))
    my_attempt = user_answers[-1] if user_answers else ""
    content = (
        f"【口试复盘】主题：{topic}。本场暴露的薄弱点：{weak}。\n"
        f"{p['card_local']}"
    )
    try:
        content = _ai_card_content(transcript, topic, weak, sbj)
    except Exception as exc:
        LOG.warning("口试复习卡 AI 生成失败，使用本地模板: %s", exc)
    return {
        "title": f"{topic}（口试复盘）",
        "content": content,
        "topic": topic,
        "subject": sbj,
        "error_type": "concept_misunderstood",
        "my_attempt": my_attempt,
        "tags": [topic],
    }


def _ai_card_content(transcript: list[dict[str, str]], topic: str, weak: str, subject: str = "") -> str:
    p = _subject_profile(subject)
    messages = [
        {"role": "system", "content": (
            p["oral_card_author"]
            + f"围绕薄弱点「{weak}」设计，要求学生用自己的话重述概念并辨析反例。只输出题目正文，不要其他内容。"
        )},
        {"role": "user", "content": "口试记录：" + json.dumps(transcript[-8:], ensure_ascii=False)},
    ]
    return call_ai(messages, max_tokens=260, tier="heavy", route="oral")
