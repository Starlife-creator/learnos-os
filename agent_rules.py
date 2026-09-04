"""智能体规则编排层（§32，降级态）。

设计哲学（与方案一致）：
- **离线优先 / 降级态**：核心编排由纯启发式规则完成，零外部依赖、无网络即可运行。
- **可选 AI 增强**：当配置了 API Key 时，可用 AI 将规则产出合成为自然语言计划，
  但 AI 仅为「装饰层」，不参与任何决策分支，断网/无 Key 时功能等价。
- 每条规则返回带优先级(0-100)的建议，编排器按优先级与去重合并为今日行动清单。

新增学科无需改动本文件：规则全部基于 db 聚合指标（due 数、薄弱主题、连续天数、错因分布）。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from config import LOG
from db import row, rows

# 规则优先级档位
P_CRIT = 100   # 考试在即 / 大量 overdue
P_HIGH = 80
P_MED = 55
P_LOW = 35


def _ctx(subject: str) -> dict[str, Any]:
    today = date.today().isoformat()
    stats = row(
        "SELECT COUNT(*) AS total, COALESCE(AVG(mastery),0) AS avg_mastery, "
        "COALESCE(SUM(CASE WHEN mastery>=4 THEN 1 ELSE 0 END),0) AS mastered "
        "FROM problems WHERE subject = ?", (subject,)
    ) or {"total": 0, "avg_mastery": 0, "mastered": 0}
    due = row(
        "SELECT COUNT(*) AS c FROM reviews WHERE completed=0 AND due_date<=? "
        "AND problem_id IN (SELECT id FROM problems WHERE subject=?)",
        (today, subject),
    ) or {"c": 0}
    weak = rows(
        "SELECT topic, COUNT(*) AS count, ROUND(AVG(mastery),1) AS mastery "
        "FROM problems WHERE subject=? AND topic<>'' GROUP BY topic "
        "ORDER BY mastery ASC, count DESC LIMIT 3", (subject,)
    )
    return {
        "subject": subject, "today": today,
        "total": int(stats["total"]), "avg_mastery": float(stats["avg_mastery"]),
        "mastered": int(stats["mastered"]),
        "due": int(due["c"]), "weak": [dict(w) for w in weak],
    }


def _rule_due(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    if ctx["due"] <= 0:
        return []
    prio = P_CRIT if ctx["due"] >= 20 else P_HIGH
    return [{
        "id": "review_due", "priority": prio,
        "action": f"复习 {ctx['due']} 张到期卡片",
        "reason": "到期卡片会按遗忘曲线快速贬值，优先清空。",
        "est_minutes": min(60, max(5, ctx["due"] * 2)),
    }]


def _rule_weak(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for w in ctx["weak"]:
        if w["mastery"] >= 3.0:
            continue
        out.append({
            "id": f"weak_{w['topic']}", "priority": P_MED,
            "action": f"精练薄弱主题「{w['topic']}」（平均掌握 {w['mastery']}/5）",
            "reason": "掌握度低于 3 的主题是考试失分高发区，优先补强。",
            "est_minutes": 15,
        })
    return out


def _rule_micro(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """微学习节奏（§42.2）：总题量够但单次投入少时，建议 ≤10 分钟微单元。"""
    if ctx["total"] < 5:
        return []
    return [{
        "id": "micro_session", "priority": P_LOW,
        "action": "今日做 1 个 ≤10 分钟微学习单元（单题深挖或 3 卡速刷）",
        "reason": "微节奏可对抗 85% 三周流失，降低启动门槛。",
        "est_minutes": 10,
    }]


def _rule_cold_start(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    if ctx["total"] == 0:
        return [{
            "id": "cold_start", "priority": P_HIGH,
            "action": f"为学科「{ctx['subject']}」录入第一道错题或读书卡片",
            "reason": "空库无法生成复习计划，先沉淀首批素材。",
            "est_minutes": 10,
        }]
    return []


def _rule_low_mastery_shallow(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    if ctx["total"] > 0 and ctx["avg_mastery"] < 2.0:
        return [{
            "id": "foundation", "priority": P_HIGH,
            "action": "降低难度、回到基础：用费曼讲解法复述 1 个核心概念",
            "reason": "平均掌握度偏低，说明地基不稳，先求懂不求快。",
            "est_minutes": 12,
        }]
    return []


RULES = [_rule_cold_start, _rule_due, _rule_low_mastery_shallow,
         _rule_weak, _rule_micro]


def orchestrate(subject: str) -> dict[str, Any]:
    """返回今日行动清单（按优先级降序）。零依赖、离线可用。"""
    ctx = _ctx(subject)
    suggestions: list[dict[str, Any]] = []
    for rule in RULES:
        suggestions.extend(rule(ctx))
    suggestions.sort(key=lambda s: s["priority"], reverse=True)
    total_min = sum(s.get("est_minutes", 0) for s in suggestions)
    return {
        "subject": subject,
        "generated_at": ctx["today"],
        "mode": "degraded",   # 降级态：纯规则，无 AI
        "context": {k: ctx[k] for k in ("total", "due", "avg_mastery", "mastered")},
        "suggestions": suggestions,
        "total_est_minutes": total_min,
    }


def synthesize_plan(subject: str, use_ai: bool = False) -> dict[str, Any]:
    """可选 AI 增强：将规则清单合成为自然语言计划。

    AI 仅做文本润色；若 use_ai 为 False 或不可用，直接返回结构化清单（功能等价）。
    """
    plan = orchestrate(subject)
    if not use_ai:
        return plan
    try:
        from ai import call_ai, get_cached_settings
        if not get_cached_settings().get("api_key"):
            return plan
        items = "\n".join(f"- {s['action']}（{s['est_minutes']}分钟）" for s in plan["suggestions"])
        prompt = (
            "你是学习教练。把下面的行动清单改写成一段鼓励性、可执行的中文今日计划，"
            "不要新增清单外的内容：\n" + items
        )
        # call_ai 的 messages 形参是 list[dict[str, str]]；此前误传裸 str，
        # 端点返回 400 后被下方 except 静默吞掉 → AI 增强从未真正生效（恒 mode="degraded"）。
        narrative = call_ai(
            [{"role": "user", "content": prompt}],
            max_tokens=400,
            route="agent",
        )
        plan["narrative"] = narrative.strip()
        plan["mode"] = "ai_enhanced"
    except Exception as exc:
        # AI 失败不影响降级功能，但必须留痕——静默 pass 曾让本 bug 长期不可见。
        LOG.debug("AI 增强计划生成失败，降级为离线模板: %s", exc)
    return plan
