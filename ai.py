"""AI 调用层：OpenAI 兼容接口、提示词构造、降级提示。"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from config import LOG, API_KEY_ENV, API_BASE_ENV, MODEL_ENV
from db import settings_dict

_settings_cache: dict[str, str] | None = None
_settings_cache_time: float = 0
_CACHE_TTL = 30  # 秒

# 内存密钥：通过 UI 录入时仅存于内存，绝不写入数据库。
_runtime_key: str | None = None


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


def get_cached_settings() -> dict[str, str]:
    """带 TTL 的设置缓存，按 环境变量 > 内存密钥 > 数据库 合并。"""
    global _settings_cache, _settings_cache_time
    if _settings_cache is not None and time.time() - _settings_cache_time <= _CACHE_TTL:
        return _settings_cache
    s = dict(settings_dict(include_secret=True))
    if API_KEY_ENV:
        s["api_key"] = API_KEY_ENV
        s["key_source"] = "environment"
    elif _runtime_key:
        s["api_key"] = _runtime_key
        s["key_source"] = "runtime"
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
    if API_KEY_ENV:
        source = "environment"
    elif _runtime_key:
        source = "runtime"
    elif has_key:
        source = "local"
    else:
        source = "none"
    return {
        "api_base": eff.get("api_base", ""),
        "model": eff.get("model", ""),
        "temperature": eff.get("temperature", "0.3"),
        "has_api_key": has_key,
        "key_source": source,
    }


def api_endpoint(base: str) -> str:
    base = base.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def call_ai(messages: list[dict[str, str]], max_tokens: int = 700, retries: int = 1) -> str:
    config = get_cached_settings()
    api_key = config.get("api_key", "").strip()
    model = config.get("model", "").strip()
    base = config.get("api_base", "").strip()
    if not api_key or not model or not base:
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

    raise last_error if last_error else RuntimeError("AI 调用失败")


def problem_prompt(problem: dict[str, Any], level: int) -> list[dict[str, str]]:
    level_rules = {
        1: "只指出应该检查的概念或最早可能出错的位置，不给公式答案，最多100字。",
        2: "给出解题方向和关键关系，但留出至少一个关键步骤让学生完成，最多180字。",
        3: "给出较完整的解题框架、检查方法和下一步，但不要代写最终作业，最多300字。",
    }
    return [
        {
            "role": "system",
            "content": (
                "你是严格而耐心的大学物理助教。你的任务是促进主动学习，不是替学生交作业。"
                "优先检查物理模型、适用条件、量纲、边界条件和极限情况。使用中文和清晰的 LaTeX。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"课程：{problem['course']}\n知识点：{problem['topic']}\n题目：{problem['content']}\n"
                f"我的尝试：{problem['my_attempt'] or '尚未提供'}\n"
                f"这是第 {level} 级提示。要求：{level_rules[level]}"
            ),
        },
    ]


def fallback_hint(problem: dict[str, Any], level: int) -> str:
    topic = problem.get("topic") or "这个问题"
    attempt = problem.get("my_attempt", "").strip()
    course = (problem.get("course") or "").strip()

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
    return '请写出最小方程组，先求符号表达式，再代入数值。最后用量纲、特殊极限和数量级做三重检查；把仍卡住的具体一步补充到「我的尝试」中。'
