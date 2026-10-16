"""AI 应用层：对外入口（标签提取/变式题/出题/审题/评分）与三层再导出。

由原 ai.py（1882 行）拆出（ADR 0008）：
  ai_client.py   传输层：结果缓存、设置与密钥、HTTP 调用、模型预设
  ai_prompts.py  提示词层：学科档案、阶段提示词、结构化 schema
  ai_fallback.py 降级层：离线启发式提示与产出
本模块只保留编排逻辑，并 re-export 三层旧名，`from ai import X` / `ai.X` 全部不变。
"""
from __future__ import annotations

import json
from typing import Any

from config import LOG
from validate import validate_object, SchemaError

import ai_client
import ai_fallback
import ai_prompts
# 注意：可变模块级状态（计数器、缓存字典、运行时密钥…）**不**在这里 from-import，
# 否则拿到的是导入瞬间的值拷贝。它们由下方 __getattr__ 逐层回退，读到的是实时值。
from ai_client import (  # noqa: F401  re-export，保持旧 import 路径可用
    _RESULT_CACHE_TTL, _CACHE_MAX_MEM, _inc_cache, get_cache_metrics, _ensure_cache_table,
    cache_get, cache_set, _CACHE_TTL, invalidate_settings_cache, set_runtime_key,
    set_master_password, unlock_keyfile, reset_session_key, clear_session_key,
    get_cached_settings, display_settings, runtime_key_present, estimate_tokens, api_endpoint,
    is_local_endpoint, _PROBE_CACHE_TTL, probe_ollama, _resolve_model, _safe_temperature,
    _clamp_temperature, _MODEL_PRESETS, _model_preset_body, PROMPT_VERSION,
    _with_cache_breakpoint, _prepare_ai_request, _target_is_private, _check_ai_target,
    _NoRedirect, _OPENER, _cached_tokens, _RETRY_BASE_DELAY, _RETRY_MAX_DELAY, _retry_sleep,
    _post_json, call_ai, call_ai_vision, embed, call_ai_stream,
)
from ai_prompts import (  # noqa: F401
    _SUBJECT_DISPLAY, _SUBJECT_ALIASES, _subject_display, _resolve_subject,
    _GENERIC_STAGE_LABELS, _GENERIC_STAGE_PROMPTS, _GENERIC_DEEPER_PROMPTS,
    _GENERIC_WEAK_PATTERNS, _GENERIC_ERR_CHECK, _BASE_PROFILE, _CHEM_PROFILE, _MATH_PROFILE,
    _GENERIC_PROFILE, _SUBJECT_OVERRIDES, _subject_profile, problem_prompt, _TAG_SCHEMA,
    _VARIANT_SCHEMA, _BANK_Q_TYPES, _BANK_Q_SCHEMA, _BANK_Q_PROMPT, _REVIEW_SCHEMA,
    _REVIEW_PROMPT, _SCORE_SCHEMA, _SCORE_PROMPT,
)
from ai_fallback import (  # noqa: F401
    fallback_hint, _KNOWLEDGE_KEYWORDS, _TYPE_KEYWORDS, _METHOD_KEYWORDS, local_tags,
    local_variants, local_bank_question,
)

_LAYERS = (ai_client, ai_prompts, ai_fallback)


def __getattr__(name: str):
    """回退查找：显式再导出漏掉的名字仍可按层解析，避免拆分破坏既有调用。"""
    for _mod in _LAYERS:
        try:
            return getattr(_mod, name)
        except AttributeError:
            pass
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def explain_concept(
    name: str,
    subject: str = "physics",
    aliases: str = "",
    prereq: str = "",
    succ: str = "",
    contrast: str = "",
    context_ref: str = "",
) -> str:
    """生成概念详解（词条式释义），纯 AI，不落库；由前端编辑后决定是否保存为 explanation。

    context_ref：U2 引用面板注入的资料段落（用户确认后传入），详解须优先依据该资料，
    资料未覆盖处才用模型知识（越界内容由前端溯源标注兜底，见 X1）。
    失败抛 RuntimeError（上层 handler 捕获降级）；无 AI 配置时调用方应已通过 _ai_quota 拦截。
    """
    ctx = []
    if aliases.strip():
        ctx.append(f"别名/缩写：{aliases.strip()}")
    if prereq.strip():
        ctx.append(f"前置概念：{prereq.strip()}")
    if succ.strip():
        ctx.append(f"后继概念：{succ.strip()}")
    if contrast.strip():
        ctx.append(f"对比概念：{contrast.strip()}")
    ctx_block = "\n".join(ctx) if ctx else "（无额外上下文）"
    ref_block = ""
    if context_ref.strip():
        ref_block = (
            f"\n\n引用资料（优先依据；资料未讲的内容需基于学科常识并保持简洁准确）：\n"
            f"{context_ref.strip()}"
        )
    prompt = (
        f"你是{subject}学科助教。请为概念「{name}」写一段极简、准确、面向学生的概念详解。\n"
        f"硬性要求：\n"
        f"1. 禁止复述它属于哪个学科/章节/单元（界面已显示，复述是冗余，反而干扰学习）；\n"
        f"2. 禁止说教口吻（如「如果你是初学者…」「入门阶段…」「建议先…」），直接给知识；\n"
        f"3. 用清晰短句，可分 2-4 行：第一行给核心定义；若上下文给出了前置/后继概念，"
        f"用一行点出关系；如有典型直觉或易错点，用一行简要点出；\n"
        f"4. 总字数控制在 80-160 字，不要 Markdown 标题，纯文本换行即可。\n"
        f"已知上下文：\n{ctx_block}{ref_block}"
    )
    return call_ai([{"role": "user", "content": prompt}], max_tokens=500, tier="heavy", route="material")
def extract_tags(
    title: str,
    content: str,
    course: str = "",
    topic: str = "",
    subject: str = "",
) -> dict[str, Any]:
    """B5 自动标签：AI 提取（C4 校验）→ 失败自动降级关键词规则。

    返回 {"tags": [...], "confidence": float, "source": "ai"|"local"}。
    仅返回建议，不落库（R3 草稿确认由调用方控制）。
    """
    sbj = _resolve_subject(subject, course, topic)
    p = _subject_profile(sbj)
    user_text = (
        f"课程：{course or '未知'}\n已有知识点：{topic or '无'}\n"
        f"题目标题：{title}\n题目内容：{content}"
    )
    # 结果缓存：同内容标签提取结果稳定 → 命中省 token
    cache_key = f"tags:{sbj}:{title}:{content[:2000]}"
    cached = cache_get(cache_key)
    if cached:
        cached["source"] = "cache"
        return cached
    prompt = [
        {"role": "system", "content": p["tag_extractor"]},
        {"role": "user", "content": user_text},
    ]
    try:
        raw = call_ai(prompt, max_tokens=300, tier="fast", retries=1, json_mode=True)
        data = validate_object(raw, _TAG_SCHEMA)
        tags = [str(t).strip() for t in data["tags"] if str(t).strip()]
        if not tags:
            raise SchemaError("标签列表为空")
        confidence = float(data["confidence"])
        if confidence < 0.9:
            return {"tags": tags, "confidence": round(confidence, 2), "source": "ai", "pending": True}
        result = {"tags": tags, "confidence": round(confidence, 2), "source": "ai"}
        cache_set(cache_key, result)
        return result
    except (SchemaError, ValueError) as exc:
        LOG.warning("AI 标签提取校验失败，降级关键词: %s", exc)
        return local_tags(title, content, course, topic, subject)
    except Exception as exc:
        LOG.warning("AI 标签提取失败，降级关键词: %s", exc)
        return local_tags(title, content, course, topic, subject)
def generate_variants(problem: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """A4 变式生成：AI（C4 校验，不一致不返回）→ 失败降级离线模板。"""
    sbj = _resolve_subject(
        problem.get("subject", ""),
        problem.get("course", ""),
        problem.get("topic", ""),
    )
    p = _subject_profile(sbj)
    prompt = [
        {"role": "system", "content": p["variant_author"]},
        {"role": "user", "content": (
            f"原题：{problem.get('content', '')}\n标题：{problem.get('title', '')}\n"
            f"知识点：{problem.get('topic', '')}\n错因：{problem.get('error_type', '')}"
        )},
    ]
    try:
        raw = call_ai(prompt, max_tokens=1500, tier="heavy", retries=1, json_mode=True)
        data = validate_object(raw, _VARIANT_SCHEMA)
        variants = data["variants"]
        if not variants:
            raise SchemaError("变式列表为空")
        return "ai", [{
            "mode": str(v["mode"]).strip(),
            "title": str(v["title"]).strip(),
            "content": str(v["content"]).strip(),
            "answer": str(v["answer"]).strip(),
            **({"type": str(v["type"]).strip()} if v.get("type") else {}),
            **({"choices": [str(c).strip() for c in v["choices"]]} if v.get("choices") else {}),
        } for v in variants]
    except (SchemaError, ValueError) as exc:
        LOG.warning("AI 变式生成校验失败，降级模板: %s", exc)
        return "local", local_variants(problem, sbj)
    except Exception as exc:
        LOG.warning("AI 变式生成失败，降级模板: %s", exc)
        return "local", local_variants(problem, sbj)
def generate_bank_question(subject: str, topic: str, qtype: str = "single",
                           context: str = "") -> dict[str, Any]:
    """AI 按题型出题，返回与 bank 模型一致的题目 dict；失败降级 local。"""
    if qtype not in _BANK_Q_TYPES:
        qtype = "single"
    p = _subject_profile(subject)
    user_text = (
        f"学科：{subject}\n知识点：{topic or '自选'}\n题型：{qtype}\n"
        f"背景材料：{context or '无'}"
    )
    prompt = [
        {"role": "system", "content": _BANK_Q_PROMPT},
        {"role": "user", "content": user_text},
    ]
    try:
        raw = call_ai(prompt, max_tokens=1500, tier="heavy", retries=1, json_mode=True)
        data = validate_object(raw, _BANK_Q_SCHEMA)
        q = data["question"]
        qtype_r = q.get("type")
        if qtype_r not in _BANK_Q_TYPES:
            qtype_r = "single"
        item: dict[str, Any] = {
            "type": qtype_r,
            "stem": str(q.get("stem", "")).strip(),
            "explain": str(q.get("explain", "")).strip(),
            # C3 来源标记：AI 出题成功路径带 ai_generated（导入题库后界面可区分/追溯）
            "source": "ai_generated",
            # C3 定向出题：落用户指定的知识点，导入题库后命中目标概念
            "concept": str(topic or "").strip()[:40] or "其他",
        }
        if qtype_r in ("single", "multiple"):
            item["choices"] = [str(c).strip() for c in (q.get("choices") or [])]
            item["answer"] = q.get("answer")
        elif qtype_r in ("fill", "subjective"):
            item["answer"] = q.get("answer")
        elif qtype_r == "composite":
            parts = q.get("parts") or []
            if not parts:
                raise SchemaError("composite 缺少 parts")
            item["parts"] = parts
        return item
    except (SchemaError, ValueError) as exc:
        LOG.warning("AI 出题校验失败，降级: %s", exc)
        return local_bank_question(subject, topic, qtype)
    except Exception as exc:
        LOG.warning("AI 出题失败，降级: %s", exc)
        return local_bank_question(subject, topic, qtype)
def ai_configured() -> bool:
    """AI 是否真正可用（能拿到有效密钥或本地端点），而非只看密钥文件是否存在。

    修复：keys.enc 存在但未解锁（口令未设置/错误）时，旧实现仍返回 True，
    导致 UI 显示 AI 可用而实际请求 401/失败、提取报"JSON 解析失败"误导用户。
    现在改为检查合并后的设置是否含非空 api_key（或本地 Ollama 等允许无钥端点）。
    """
    try:
        s = get_cached_settings()
        key = (s.get("api_key") or "").strip()
        if key:
            return True
        base = (s.get("api_base") or "").strip()
        # 本地端点（Ollama 等）允许空密钥
        return bool(base) and is_local_endpoint(base)
    except Exception:
        return runtime_key_present()
def review_bank_question(question: dict[str, Any], subject: str = "") -> dict[str, Any]:
    """A6 AI 审题：校验出一题质量。

    返回 {verdict, issues, comment, revised?, ai_available}。
    AI 不可用/异常时降级为 {verdict: "pass", ai_available: False}，不阻断出题流程。
    """
    try:
        q = dict(question or {})
        q.setdefault("type", q.get("type") or "single")
        if not ai_configured():
            return {"verdict": "pass", "issues": [], "comment": "", "ai_available": False}
    except Exception:
        return {"verdict": "pass", "issues": [], "comment": "", "ai_available": False}
    payload = json.dumps(q, ensure_ascii=False)
    # 结果缓存：同题同学科审题结果稳定 → 命中直接返回（省 token）
    cache_key = f"review:{subject or ''}:{payload}"
    cached = cache_get(cache_key)
    if cached:
        cached["ai_available"] = True
        cached["cached"] = True
        return cached
    prompt = [
        {"role": "system", "content": _REVIEW_PROMPT},
        {"role": "user", "content": f"学科：{subject or '未知'}\n待审题目：\n{payload}"},
    ]
    try:
        raw = call_ai(prompt, max_tokens=700, tier="heavy", retries=1, json_mode=True)
        data = validate_object(raw, _REVIEW_SCHEMA)
        rv = data["review"]
        verdict = rv.get("verdict", "warn")
        if verdict not in ("pass", "warn", "reject"):
            verdict = "warn"
        result = {
            "verdict": verdict,
            "issues": [str(x) for x in (rv.get("issues") or [])][:8],
            "comment": str(rv.get("comment") or "").strip(),
            "revised": str(rv.get("revised") or "").strip() or None,
            "ai_available": True,
        }
        cache_set(cache_key, {k: v for k, v in result.items() if k != "ai_available"})
        return result
    except (SchemaError, ValueError) as exc:
        LOG.warning("AI 审题校验失败，降级: %s", exc)
    except Exception as exc:
        LOG.warning("AI 审题失败，降级: %s", exc)
    return {"verdict": "pass", "issues": [], "comment": "", "ai_available": False}
def ai_score_item(item: dict[str, Any], user_raw: Any, subject: str = "") -> dict[str, Any]:
    """A7 AI 评分（递归）。

    - 客观题叶（single/multiple/fill）：复用 grade_item 确定性判分，score=100 或 0；
    - 主观题叶（subjective）：AI 可用则调 AI 给 0-100 分＋点评；否则 score=None, needs_review=True；
    - composite：递归聚合，按权重（客观=选项数/空数，主观=100）加权平均；
    - AI 不可用/失败：对应主观标记 needs_review，不拉低总分分母。
    返回 {score(0-100 或 None), ai_available, mode, needs_review, comment?, against?, parts?}。
    """
    from bank import grade_item

    def _res(t: str, score: int | None, mode: str, *, avail: bool = False,
             need: bool = False, weight: int = 1, **extra: Any) -> dict[str, Any]:
        d: dict[str, Any] = {
            "type": t, "score": score, "mode": mode,
            "ai_available": avail, "needs_review": need, "weight": weight,
        }
        d.update(extra)
        return d

    def rec(it: dict[str, Any], ans: Any) -> dict[str, Any]:
        t = it.get("type", "single")
        if t == "composite":
            parts = it.get("parts") or []
            in_ans = ans if isinstance(ans, list) else []
            subs = [rec(p, in_ans[i] if i < len(in_ans) else None)
                    for i, p in enumerate(parts)]
            scored = [s for s in subs if s["score"] is not None]
            if not scored:
                score: int | None = None
            else:
                wsum = sum(s["weight"] for s in scored)
                score = int(round(sum(s["score"] * s["weight"] for s in scored) / (wsum or 1)))
            return _res(
                t, score, _agg_mode([s["mode"] for s in subs]),
                avail=any(s["ai_available"] for s in subs),
                need=any(s["needs_review"] for s in subs),
                weight=1, parts=subs,
            )
        if t == "multiple":
            g = grade_item(it, ans)
            w = max(len(it.get("choices") or []), 1)
            return _res(t, 100 if g["correct"] else 0, "deterministic", weight=w)
        if t == "fill":
            raw_ans = it.get("answer")
            w = len(raw_ans) if isinstance(raw_ans, list) else 1
            g = grade_item(it, ans)
            return _res(t, 100 if g["correct"] else 0, "deterministic", weight=max(w, 1))
        if t == "subjective":
            g = grade_item(it, ans)
            if g["correct"] is not None:  # 理论上有兜底，安全处理
                return _res(t, 100 if g["correct"] else 0, "deterministic")
            out = _ai_subjective(it, ans, subject)
            if out["score"] is None:
                return _res(t, None, "unrated", need=True)
            return _res(t, out["score"], "ai", avail=True, weight=100,
                        comment=out["comment"], against=out.get("against") or "")
        # single / 兜底
        g = grade_item(it, ans)
        return _res(t, 100 if g["correct"] else 0, "deterministic")

    top = rec(item, user_raw)
    out: dict[str, Any] = {
        "score": top["score"],
        "ai_available": top["ai_available"],
        "mode": top["mode"],
        "needs_review": top["needs_review"],
    }
    if item.get("type") == "composite":
        out["parts"] = top["parts"]
    if top.get("comment"):
        out["comment"] = top["comment"]
    if top.get("against"):
        out["against"] = top["against"]
    return out
def _agg_mode(modes: list[str]) -> str:
    if "ai" in modes:
        return "ai" if not any(m != "ai" for m in modes) else "mixed"
    if "unrated" in modes:
        return "unrated"
    return "deterministic"
def _ai_ready() -> bool:
    """AI 是否可用。仅记录观察结果，不缓存 False 以免新录入 key 后无法恢复。"""
    return ai_configured()
def _ai_subjective(item: dict[str, Any], user_raw: Any, subject: str) -> dict[str, Any]:
    """单道主观题 AI 评分。返回 {score(int|None), comment, against}；失败 score=None。"""
    try:
        if not _ai_ready():
            return {"score": None, "comment": "", "against": ""}
    except Exception:
        return {"score": None, "comment": "", "against": ""}
    user_text = str(user_raw or "").strip()
    ref = str(item.get("answer") or "").strip()
    if not user_text:
        # 未作答：保持待评阅语义，不自动给 0 分（避免误把“没写”当“判过”）
        return {"score": None, "comment": "", "against": ""}
    payload = json.dumps({
        "stem": str(item.get("stem") or ""),
        "answer": ref,
        "student": user_text[:2000],
        "subject": subject,
    }, ensure_ascii=False)
    prompt = [
        {"role": "system", "content": _SCORE_PROMPT},
        {"role": "user", "content": f"题目与参考答案与学生作答：\n{payload}"},
    ]
    try:
        # 第 1 次调用（低温 0）：只按 rubric 出结构化分值（评分稳定）
        raw = call_ai(prompt, max_tokens=500, tier="heavy", retries=1,
                      temperature=0.0, route="score", json_mode=True)
        data = validate_object(raw, _SCORE_SCHEMA)
        sc = data["score"]
        s = int(sc.get("score") or 0)
        s = max(0, min(100, s))
        comment = str(sc.get("comment") or "").strip()
        against = str(sc.get("against") or "").strip()
        # 第 2 次调用（较高温 0.7）：基于已定分数生成自然点评（点评活泼不呆板）
        # 失败不影响分数，仅降级用第一次的 comment。
        try:
            polish_prompt = [
                {"role": "system", "content": (
                    "你是友善的学习辅导老师。根据【学生作答】与【参考答案】，就刚才得到的分数 "
                    f"（{s} 分）写 1-3 句中文点评：先肯定优点，再点出 1-2 个具体可改进点，语气亲切具体。"
                    "只返回 JSON：{comment: 点评}" 
                )},
                {"role": "user", "content": f"题目与答案：\n{payload}\n学生已得 {s} 分。"},
            ]
            raw2 = call_ai(polish_prompt, max_tokens=200, tier="heavy", retries=1,
                           temperature=0.7, route="score", json_mode=True)
            polished = validate_object(raw2, {"comment": {"type": "string", "required": True}})
            c2 = str(polished.get("comment") or "").strip()
            if c2:
                comment = c2
        except Exception:
            pass  # 点评增强失败 → 保留第一次的 comment
        return {"score": s, "comment": comment, "against": against}
    except (SchemaError, ValueError) as exc:
        LOG.warning("AI 评分校验失败，降级自评: %s", exc)
    except Exception as exc:
        LOG.warning("AI 评分失败，降级自评: %s", exc)
    return {"score": None, "comment": "", "against": ""}
