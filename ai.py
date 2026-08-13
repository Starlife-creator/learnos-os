"""AI 调用层：OpenAI 兼容接口、提示词构造、降级提示。"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

from config import LOG, API_KEY_ENV, API_BASE_ENV, MODEL_ENV
from db import settings_dict
from keystore import load_key, key_file_exists
from validate import validate_object, SchemaError

_settings_cache: dict[str, str] | None = None
_settings_cache_time: float = 0
_CACHE_TTL = 30  # 秒

# 内存密钥：通过 UI 录入时仅存于内存，绝不写入数据库（R4）。
_runtime_key: str | None = None
# 可选 keys.enc 主密钥口令（用户提供，经环境变量或 UI 传递，不落库不落盘）。
_master_password: str | None = None


def invalidate_settings_cache() -> None:
    """设置更新后调用，清除缓存。"""
    global _settings_cache, _settings_cache_time
    _settings_cache = None
    _settings_cache_time = 0


def set_runtime_key(key: str | None) -> None:
    """设置内存密钥（会话级，重启后失效）。"""
    global _runtime_key
    _runtime_key = (key or "").strip() or None
    invalidate_settings_cache()


def set_master_password(password: str | None) -> None:
    """设置 keys.enc 主密钥口令（会话级）。"""
    global _master_password
    _master_password = (password or "").strip() or None
    invalidate_settings_cache()


def get_cached_settings() -> dict[str, str]:
    """带 TTL 的设置缓存，按 环境变量 > keys.enc > 内存密钥 > DB(非敏感) 合并。"""
    global _settings_cache, _settings_cache_time
    if _settings_cache is not None and time.time() - _settings_cache_time <= _CACHE_TTL:
        return _settings_cache
    s = dict(settings_dict(include_secret=True))
    if API_KEY_ENV:
        s["api_key"] = API_KEY_ENV
        s["key_source"] = "environment"
    elif key_file_exists() and _master_password:
        stored = load_key(_master_password)
        if stored:
            s["api_key"] = stored
            s["key_source"] = "keyfile"
        else:
            s["api_key"] = _runtime_key or ""
            s["key_source"] = "runtime" if _runtime_key else "none"
    elif _runtime_key:
        s["api_key"] = _runtime_key
        s["key_source"] = "runtime"
    else:
        s["api_key"] = ""
        s["key_source"] = "none"
    if API_BASE_ENV:
        s["api_base"] = API_BASE_ENV
    if MODEL_ENV:
        s["model"] = MODEL_ENV
    _settings_cache = s
    _settings_cache_time = time.time()
    return s


def display_settings() -> dict[str, str]:
    """供设置页展示：脱敏后的有效配置与密钥来源。"""
    eff = get_cached_settings()
    has_key = bool(eff.get("api_key"))
    return {
        "api_base": eff.get("api_base", ""),
        "model": eff.get("model", ""),
        "fast_model": eff.get("fast_model", ""),
        "heavy_model": eff.get("heavy_model", ""),
        "vision_model": eff.get("vision_model", ""),
        "temperature": eff.get("temperature", "0.3"),
        "has_api_key": has_key,
        "key_source": eff.get("key_source", "none"),
    }


def api_endpoint(base: str) -> str:
    base = base.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def is_local_endpoint(base: str) -> bool:
    """本地模型（Ollama 等）端点允许空密钥（C3）。"""
    base = (base or "").strip().lower()
    return base.startswith("http://localhost") or base.startswith("http://127.0.0.1") or base.startswith("http://[::1]")


def probe_ollama(timeout: float = 1.5) -> dict[str, Any] | None:
    """C3 探测本地 Ollama 服务（用户自装，仅探测不安装）。失败返回 None。"""
    try:
        request = urllib.request.Request(
            "http://localhost:11434/api/tags", method="GET",
            headers={"User-Agent": "PhysicsStudyOS/0.3"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        models = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
        return {"available": True, "models": models}
    except Exception as exc:
        LOG.debug("Ollama 探测失败: %s", exc)
        return None


def _resolve_model(config: dict[str, str], tier: str | None) -> str:
    """C2 双档：fast/heavy 覆盖默认 model；未配置则回退默认。"""
    model = config.get("model", "").strip()
    if tier in ("fast", "heavy"):
        tier_model = config.get(f"{tier}_model", "").strip()
        if tier_model:
            return tier_model
    return model


def call_ai(
    messages: list[dict[str, str]],
    max_tokens: int = 700,
    retries: int = 1,
    tier: str | None = None,
    model_override: str | None = None,
    route: str = "",
) -> str:
    from telemetry import record
    start = time.monotonic()
    config = get_cached_settings()
    api_key = config.get("api_key", "").strip()
    model = _resolve_model(config, tier)
    if model_override:
        model = model_override
    base = config.get("api_base", "").strip()
    if not model or not base:
        record(route=route, model=model, ok=False, error_kind="not_configured",
               start=start)
        raise ValueError('请先在「AI 设置」中填写 API 地址、密钥和模型。')
    if not api_key and not is_local_endpoint(base):
        record(route=route, model=model, ok=False, error_kind="not_configured",
               start=start)
        raise ValueError('请先在「AI 设置」中填写 API 地址、密钥和模型。')

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": float(config.get("temperature", "0.3")),
        "max_tokens": max_tokens,
    }, ensure_ascii=False).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "PhysicsStudyOS/0.2",
    }

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(
            api_endpoint(base), data=payload, headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.loads(response.read().decode("utf-8"))
            usage = result.get("usage") or {}
            tokens = int(usage.get("total_tokens") or 0)
            record(route=route, model=model, ok=True, tokens=tokens, start=start)
            return result["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            last_error = RuntimeError(f"AI 接口返回 {exc.code}：{detail}")
            if exc.code < 500:
                break
        except urllib.error.URLError as exc:
            last_error = RuntimeError(f"无法连接 AI 接口：{exc.reason}")
        except (KeyError, IndexError, TypeError) as exc:
            last_error = RuntimeError("AI 接口响应格式不兼容。")
            break
        if attempt < retries:
            LOG.warning("AI 调用第 %d 次失败，正在重试...", attempt + 1)

    record(route=route, model=model, ok=False,
           error_kind=type(last_error).__name__ if last_error else "unknown",
           start=start)
    raise last_error if last_error else RuntimeError("AI 调用失败")


def call_ai_vision(text: str, image_data_uri: str, max_tokens: int = 900) -> str:
    """B1 视觉识别：以 image_url 数据 URI 调用配置的 vision 模型（OpenAI 兼容）。"""
    config = get_cached_settings()
    model = config.get("vision_model", "").strip() or config.get("model", "").strip()
    if not model:
        raise ValueError("未配置视觉模型，请在「AI 设置」中填写 vision_model")
    messages = [{"role": "user", "content": [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": image_data_uri}},
    ]}]
    return call_ai(messages, max_tokens=max_tokens, tier="heavy", model_override=model)


def call_ai_stream(
    messages: list[dict[str, str]],
    max_tokens: int = 700,
    tier: str | None = None,
    route: str = "",
) -> Any:
    """C1 流式调用：请求 stream=true，返回 (生成器逐块文本, 超时重试语义)。

    生成器每步产出增量文本；调用方负责关闭响应。
    """
    from telemetry import record
    start = time.monotonic()
    config = get_cached_settings()
    api_key = config.get("api_key", "").strip()
    model = _resolve_model(config, tier)
    base = config.get("api_base", "").strip()
    if not model or not base:
        record(route=route, model=model, ok=False, error_kind="not_configured",
               start=start)
        raise ValueError('请先在「AI 设置」中填写 API 地址、密钥和模型。')
    if not api_key and not is_local_endpoint(base):
        record(route=route, model=model, ok=False, error_kind="not_configured",
               start=start)
        raise ValueError('请先在「AI 设置」中填写 API 地址、密钥和模型。')

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": float(config.get("temperature", "0.3")),
        "max_tokens": max_tokens,
        "stream": True,
    }, ensure_ascii=False).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "PhysicsStudyOS/0.2",
    }
    request = urllib.request.Request(
        api_endpoint(base), data=payload, headers=headers, method="POST",
    )
    try:
        response = urllib.request.urlopen(request, timeout=120)
    except Exception as exc:
        record(route=route, model=model, ok=False,
               error_kind=type(exc).__name__, start=start)
        raise

    def _chunks():
        try:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    piece = json.loads(data)
                    delta = piece["choices"][0]["delta"].get("content", "")
                except (KeyError, IndexError, TypeError, json.JSONDecodeError):
                    delta = ""
                if delta:
                    yield delta
            record(route=route, model=model, ok=True, start=start)
        except Exception as exc:
            record(route=route, model=model, ok=False,
                   error_kind=type(exc).__name__, start=start)
            raise
        finally:
            response.close()

    return _chunks()


def problem_prompt(problem: dict[str, Any], level: int, lang: str = "zh") -> list[dict[str, str]]:
    level_rules = {
        1: "只指出应该检查的概念或最早可能出错的位置，不给公式答案，最多100字。",
        2: "给出解题方向和关键关系，但留出至少一个关键步骤让学生完成，最多180字。",
        3: "给出较完整的解题框架、检查方法和下一步，但不要代写最终作业，最多300字。",
        4: "给出完整解析：列清步骤、关键公式与最终结果，但最后补一句『自己重做一遍』的引导，最多400字。",
    }
    level_rules_en = {
        1: "Only point out the concept to check or the earliest likely error location; no formula answers, at most 100 words.",
        2: "Give the solving direction and key relations, but leave at least one key step for the student to finish, at most 180 words.",
        3: "Give a fairly complete solving framework, checking methods and next steps, but do not write the final answer for the student, at most 300 words.",
        4: "Give the full solution: list the steps, key formulas and final result, but end with a prompt to redo it yourself, at most 400 words.",
    }
    # C6：按错因定向检查（提高提示针对性）
    error_line = ""
    try:
        from errors import ERROR_TYPE_LABELS, normalize_error_type
        et = normalize_error_type(problem.get("error_type"))
        if et in ERROR_TYPE_LABELS:
            _ERR_CHECK: dict[str, str] = {
                "概念理解错误": "先帮学生核对物理模型与适用条件，指出概念误区，不直接给公式。",
                "计算错误": "请复核运算过程与量纲，指出第几步可能出错并引导重算。",
                "粗心笔误": "提示按步骤检查符号、单位和抄写，提醒这类失误最容易在符号正负号。",
                "时间压力": "提醒先建立最少必要步骤的解题顺序，并给出抢分优先级建议。",
                "审题错误": "指出题目中容易被忽略的条件和关键词，引导重新读题。",
                "公式/事实空白": "给出提示公式的适用边界与推导线索，帮助回忆而非背诵。",
                "直觉陷阱": "提示先用特例或极限检验直觉，指出反直觉点，不要直接判对错。",
            }
            error_line = f"（该学生标记的错因：{ERROR_TYPE_LABELS[et]}。针对性要求：{_ERR_CHECK[ERROR_TYPE_LABELS[et]]}）"
    except Exception as exc:
        LOG.debug("错因提示构造失败（可忽略）: %s", exc)
    # C5：AI 请求上下文附加学习者档案（隐私仅本地；失败不影响主流程）
    profile_line = ""
    try:
        from profile import snapshot
        profile_line = snapshot()
    except Exception as exc:
        LOG.debug("档案快照生成失败（可忽略）: %s", exc)
    if lang == "en":
        return [
            {
                "role": "system",
                "content": "You are a strict but patient university physics TA. Your job is to promote active "
                           "learning, not to do the homework for the student. "
                           "Prioritize checking the physical model, applicable conditions, dimensions, "
                           "boundary conditions and limiting cases. Answer in English with clear LaTeX."
                           f"{profile_line}",
            },
            {
                "role": "user",
                "content": (
                    f"Course: {problem['course']}\nTopic: {problem['topic']}\nProblem: {problem['content']}\n"
                    f"My attempt: {problem['my_attempt'] or 'Not provided yet'}\n"
                    f"Level {level} hint. Requirement: {level_rules_en[level]}"
                ),
            },
        ]
    return [
        {
            "role": "system",
            "content": "你是严格而耐心的大学物理助教。你的任务是促进主动学习，不是替学生交作业。"
                       "优先检查物理模型、适用条件、量纲、边界条件和极限情况。使用中文和清晰的 LaTeX。"
                       f"{profile_line}",
        },
        {
            "role": "user",
            "content": (
                f"课程：{problem['course']}\n知识点：{problem['topic']}\n题目：{problem['content']}\n"
                f"我的尝试：{problem['my_attempt'] or '尚未提供'}\n"
                f"{error_line}\n"
                f"这是第 {level} 级提示。要求：{level_rules[level]}"
            ),
        },
    ]


def fallback_hint(problem: dict[str, Any], level: int, lang: str = "zh") -> str:
    topic = problem.get("topic") or ("这个问题" if lang == "zh" else "this problem")
    attempt = problem.get("my_attempt", "").strip()
    course = (problem.get("course") or "").strip()

    if lang == "en":
        extra = ("You have not recorded your attempt yet — first draw a diagram or write the basic equations."
                 if not attempt else "Start from the first basic equation you wrote and label each term's source and sign.")
        if level == 1:
            return (f"Do not calculate yet. Clarify the object of study, the known and unknown quantities, and the "
                    f"applicable conditions for \"{topic}\"; then check that every equation you wrote is "
                    f"dimensionally consistent.")
        if level == 2:
            return (f"Break the problem into: build a model → choose coordinates / conserved quantities → write the "
                    f"basic equations → check boundary conditions. {extra}")
        if level == 3:
            return ("Write the minimal set of equations, solve symbolically first, then plug in numbers. "
                    "Do a triple check with dimensions, special limits and orders of magnitude; add the exact step "
                    "where you get stuck to \"My Attempt\".")
        return (f"Full solution framework: 1) identify the object and known/unknown quantities; "
                f"2) choose the physical model ({topic}) and state its applicable conditions; "
                "3) write the basic equations and check signs and dimensions line by line; "
                "4) solve symbolically first, then substitute numbers; "
                "5) verify with limiting cases (mass→0, force→∞) and orders of magnitude. "
                "Compare key formulas and the final result with a standard solution — redo it yourself before checking.")

    # 知识点感知模板：根据课程/主题关键词匹配更精准的指导
    _TOPIC_HINTS: dict[str, tuple[str, str, str]] = {
        "力学": (
            f"先给「{topic}」中的每个物体画受力图，标出全部受力与正方向，再检查是否遗漏了约束反力或摩擦力。",
            "从你写出的第一条基本方程开始，逐项标注来源（牛顿第二定律 / 动量守恒 / 动能定理）和正方向。" if attempt else "画完受力图后写出每个物体的运动方程；检查坐标系是否统一、质量是否区分。",
            "列出牛顿定律或守恒律的最小方程组，先求符号表达式，再代数值。最后检查极限情况（质量→0、外力→∞）和量纲。",
        ),
        "电磁学": (
            f"先明确「{topic}」中的电荷分布 / 电流构型 / 磁场源，画出场线或等效电路图；再检查对称性和适用条件。",
            "从高斯定理或安培环路定律出发，标注哪个面对称/轴对称；区分电场 E 和磁场 B 的方向。" if attempt else "画出电场线或磁感线分布；如果你在用电路模型，请确认每个元件两端的电压符号和参考方向。",
            "写出麦克斯韦方程组中对应的积分/微分形式，代入对称条件化简。最后检查边界条件（导体表面/介质界面）和极限情况。",
        ),
        "热学": (
            f"先明确「{topic}」中的系统边界、状态参量(P,V,T)和过程类型（等温/绝热/等压/等容）。",
            "从状态方程 PV=nRT 或第一定律 dU=δQ-δW 出发，逐项确认符号正负约定。" if attempt else "写出系统初末态的热力学参量，确认过程的可逆性和做功表达式。",
            "联立状态方程与能量方程，先求符号表达式，再代入数值。最后检查极限情况（体积→∞/→0）和量纲。",
        ),
        "光学": (
            f"先画出「{topic}」中的光路图，标记入射角、折射角、光程差；确认所用原理（几何光学/波动光学）。",
            "从折射定律或干涉条件出发，确认符号约定（实正虚负）和介质折射率。" if attempt else "画出完整光路图，标注各界面处的入射角和透射角，写下每个界面的折射/反射方程。",
            "联立光学路径方程组，先求符号表达式，检查薄透镜近似或傍轴条件是否成立。最后验算极限情况（折射率→1 退化为真空）。",
        ),
        "振动与波": (
            f"先明确「{topic}」的振动模型（简谐/阻尼/受迫）和初始条件；画出振动曲线或波的传播示意图。",
            "从运动方程 x''+ω²x=0 或波动方程出发，确认边界条件和初始相位。" if attempt else "写出系统的运动微分方程，判断是简谐振动还是阻尼振动；检查初始位移和初始速度。",
            "求解微分方程得通解 + 特解，代初条件定常数。检查极限情况（阻尼→∞ 过阻尼不振荡）和量纲。",
        ),
    }

    keywords = _TOPIC_HINTS.get(course.split("（")[0].split("(")[0].strip())
    if not keywords:
        for key, vals in _TOPIC_HINTS.items():
            if key in course or key in topic:
                keywords = vals
                break

    if keywords and level <= len(keywords):
        return keywords[level - 1]

    # 未匹配时的通用降级
    if level == 1:
        return f'先不要计算。请明确「{topic}」中研究对象、已知量、未知量与成立条件；再检查你的每个等式是否量纲一致。'
    if level == 2:
        extra = "你还没有记录自己的尝试，请先画出受力图或基本方程。" if not attempt else "从你写出的第一条基本方程开始，逐项标注来源和正方向。"
        return f"建议把问题拆成：建立模型 → 选择坐标/守恒量 → 写基本方程 → 检查边界条件。{extra}"
    if level == 3:
        return '请写出最小方程组，先求符号表达式，再代入数值。最后用量纲、特殊极限和数量级做三重检查；把仍卡住的具体一步补充到「我的尝试」中。'
    return (f'完整解析框架：1) 明确研究对象与已知/未知量；2) 选择物理模型（{topic}）并写适用条件；'
            '3) 列基本方程并逐项检查符号与量纲；4) 解出符号表达式再代数值；'
            '5) 用极限情况（质量→0、外力→∞）与数量级复核。'
            '关键公式与最终结果请对照标准解析核对——建议先自己重做一遍再对答案。')


# ── B5 自动标签 + 知识提取 ──────────────────────────────────

_TAG_SCHEMA = {
    "tags": {"type": "array", "items": {"type": "string"}, "required": True},
    "confidence": {"type": "number", "min": 0.0, "max": 1.0, "required": True},
}

# 降级词库：物理知识点关键词（按题面出现次数加权）
_KNOWLEDGE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "力学": ("牛顿", "受力", "摩擦力", "动量", "冲量", "动能", "机械能", "圆周", "平抛", "斜面", "弹簧", "质心", "角动量", "转动惯量", "简谐"),
    "电磁学": ("电场", "电势", "电容", "电流", "电阻", "欧姆", "磁感应", "安培", "洛伦兹", "法拉第", "电磁感应", "楞次", "麦克斯韦", "电感", "LC"),
    "热学": ("热力学", "温度", "压强", "内能", "熵", "绝热", "等温", "等压", "等容", "热机", "卡诺", "分子动理论", "理想气体"),
    "光学": ("折射", "反射", "衍射", "干涉", "偏振", "光程", "透镜", "全反射", "波长", "频率", "光子"),
    "振动与波": ("振动", "波动", "波速", "波长", "驻波", "多普勒", "相位", "简谐"),
    "原子物理": ("原子", "核", "能级", "跃迁", "衰变", "半衰期", "光子", "光电效应", "波尔", "量子"),
}
_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "计算题": ("求", "计算", "数值", "解得", "大小为", "等于"),
    "概念题": ("概念", "区别", "判断", "说法正确的是", "错误的是", "为什么", "原理"),
    "证明题": ("证明", "推导", "验证", "证明题"),
    "实验题": ("实验", "测量", "器材", "误差", "读数"),
}
_METHOD_KEYWORDS: dict[str, tuple[str, ...]] = {
    "受力分析": ("受力", "隔离", "整体法"),
    "守恒法": ("守恒", "能量守恒", "动量守恒"),
    "微积分": ("积分", "微分", "dt", "ds"),
}


def local_tags(title: str, content: str, course: str = "", topic: str = "") -> dict[str, Any]:
    """B5 降级：关键词规则打标签（无 AI 时可用）。置信度按匹配强度估算。"""
    text = f"{title}\n{content}"
    found: list[str] = []
    hits = 0
    if course:
        found.append(f"课程:{course}")
        hits += 1
    if topic:
        found.append(f"知识点:{topic}")
        hits += 1
    for label, words in _KNOWLEDGE_KEYWORDS.items():
        count = sum(text.count(w) for w in words)
        if count:
            found.append(f"知识点:{label}")
            hits += count
    for label, words in _TYPE_KEYWORDS.items():
        if any(w in text for w in words):
            found.append(f"题型:{label}")
            hits += 1
    for label, words in _METHOD_KEYWORDS.items():
        if any(w in text for w in words):
            found.append(f"方法:{label}")
            hits += 1
    if not found:
        found.append("知识点:待分类")
    confidence = min(0.95, 0.45 + 0.08 * hits) if hits > 0 else 0.3
    return {"tags": found, "confidence": round(confidence, 2), "source": "local"}


def extract_tags(
    title: str,
    content: str,
    course: str = "",
    topic: str = "",
) -> dict[str, Any]:
    """B5 自动标签：AI 提取（C4 校验）→ 失败自动降级关键词规则。

    返回 {"tags": [...], "confidence": float, "source": "ai"|"local"}。
    仅返回建议，不落库（R3 草稿确认由调用方控制）。
    """
    user_text = (
        f"课程：{course or '未知'}\n已有知识点：{topic or '无'}\n"
        f"题目标题：{title}\n题目内容：{content}"
    )
    prompt = [
        {"role": "system", "content": (
            "你是物理题标签提取器。根据题目提取 3-6 个标签，每项格式必须是 "
            "'知识点:名称'、'题型:名称'、'难度:易|中|难'、'方法:名称'、'错因:名称' 之一。"
            "只返回 JSON，不要多余文字。"
        )},
        {"role": "user", "content": user_text},
    ]
    try:
        raw = call_ai(prompt, max_tokens=300, tier="fast", retries=1)
        data = validate_object(raw, _TAG_SCHEMA)
        tags = [str(t).strip() for t in data["tags"] if str(t).strip()]
        if not tags:
            raise SchemaError("标签列表为空")
        confidence = float(data["confidence"])
        if confidence < 0.9:
            return {"tags": tags, "confidence": round(confidence, 2), "source": "ai", "pending": True}
        return {"tags": tags, "confidence": round(confidence, 2), "source": "ai"}
    except (SchemaError, ValueError) as exc:
        LOG.warning("AI 标签提取校验失败，降级关键词: %s", exc)
        return local_tags(title, content, course, topic)
    except Exception as exc:
        LOG.warning("AI 标签提取失败，降级关键词: %s", exc)
        return local_tags(title, content, course, topic)


# ── A4 举一反三变式题引擎 ─────────────────────────────────

_VARIANT_SCHEMA = {
    "variants": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "required": True},
                "title": {"type": "string", "min_length": 1, "required": True},
                "content": {"type": "string", "min_length": 1, "required": True},
                "answer": {"type": "string", "min_length": 1, "required": True},
            },
        },
        "required": True,
    },
}


def local_variants(problem: dict[str, Any]) -> list[dict[str, Any]]:
    """A4 降级：离线参数化变式模板（按错因×题型启发式，零依赖）。"""
    content = problem.get("content", "")
    title = problem.get("title", "")
    topic = problem.get("topic", "")
    error_type = problem.get("error_type", "")
    variants: list[dict[str, Any]] = []
    # 数值替换：把常见整数/小数换成更"丑"的数（计算错场景）
    nums = re.findall(r"\d+(?:\.\d+)?", content)
    if nums:
        swap = {"1": "7", "2": "6", "3": "5", "4": "9", "5": "2", "6": "3", "7": "8", "8": "4", "9": "1"}
        replaced = content
        for n in nums:
            if error_type in ("calculation", "careless"):
                rep = "".join(swap.get(ch, ch) for ch in n)
                replaced = replaced.replace(n, rep, 1)
        variants.append({
            "mode": "数值替换",
            "title": f"{title}（数值变式）",
            "content": replaced,
            "answer": "同原题解法，注意代入新数值后重新检查量纲与数量级。",
        })
    # 情境替换：换一个物理场景（概念/建模错）
    scene_swap = [
        ("斜面", "水平桌面"), ("小车", "木块"), ("小球", "滑块"),
        ("电梯", "火箭"), ("弹簧", "橡皮绳"), ("磁场", "电场"),
    ]
    new_content = content
    for a, b in scene_swap:
        if a in new_content:
            new_content = new_content.replace(a, b, 1)
            variants.append({
                "mode": "情境替换",
                "title": f"{title}（情境变式）",
                "content": new_content,
                "answer": "模型不变，注意新场景下边界条件与受力的差异。",
            })
            break
    # 反向设问：把"求 X"改为"给 X 反推条件"（概念错场景）
    m = re.search(r"(?:求|计算|大小为|等于)\s*([^，。；]+)", content)
    if m:
        target = m.group(1).strip()
        variants.append({
            "mode": "反向设问",
            "title": f"{title}（反向设问）",
            "content": f"给定结果 {target} = 已知值，反推题目中某一初始条件；写出推导过程并验证量纲。",
            "answer": "把原题正向方程反解为初始条件表达式，代入结果校验。",
        })
    if not variants:
        variants.append({
            "mode": "重述练习",
            "title": f"{title}（重述）",
            "content": f"不看书本，用自己的话重新表述 {topic or title} 的解题思路，并写出关键公式及适用条件。",
            "answer": "对照标准解析检查：模型选择、公式适用条件、边界条件是否完整。",
        })
    return variants


def generate_variants(problem: dict[str, Any]) -> list[dict[str, Any]]:
    """A4 变式生成：AI（C4 校验，不一致不返回）→ 失败降级离线模板。"""
    prompt = [
        {"role": "system", "content": (
            "你是物理出题助手。基于给定错题生成 3 道变式，三题模式分别为：数值替换、情境替换、反向设问。"
            "每题必须包含 mode、title、content、answer 四个字段，只返回 JSON。"
        )},
        {"role": "user", "content": (
            f"原题：{problem.get('content', '')}\n标题：{problem.get('title', '')}\n"
            f"知识点：{problem.get('topic', '')}\n错因：{problem.get('error_type', '')}"
        )},
    ]
    try:
        raw = call_ai(prompt, max_tokens=900, tier="heavy", retries=1)
        data = validate_object(raw, _VARIANT_SCHEMA)
        variants = data["variants"]
        if not variants:
            raise SchemaError("变式列表为空")
        return "ai", [{
            "mode": str(v["mode"]).strip(),
            "title": str(v["title"]).strip(),
            "content": str(v["content"]).strip(),
            "answer": str(v["answer"]).strip(),
        } for v in variants]
    except (SchemaError, ValueError) as exc:
        LOG.warning("AI 变式生成校验失败，降级模板: %s", exc)
        return "local", local_variants(problem)
    except Exception as exc:
        LOG.warning("AI 变式生成失败，降级模板: %s", exc)
        return "local", local_variants(problem)
