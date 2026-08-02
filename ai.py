"""AI 调用层：OpenAI 兼容接口、提示词构造、降级提示。"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from config import LOG
from db import settings_dict

_settings_cache: dict[str, str] | None = None
_settings_cache_time: float = 0
_CACHE_TTL = 30  # 秒


def invalidate_settings_cache() -> None:
    """设置更新后调用，清除缓存。"""
    global _settings_cache, _settings_cache_time
    _settings_cache = None
    _settings_cache_time = 0


def get_cached_settings() -> dict[str, str]:
    """带 TTL 的设置缓存，避免每次 AI 调用都查数据库。"""
    global _settings_cache, _settings_cache_time
    if _settings_cache is None or time.time() - _settings_cache_time > _CACHE_TTL:
        _settings_cache = settings_dict(include_secret=True)
        _settings_cache_time = time.time()
    return _settings_cache


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
    if level == 1:
        return f'先不要计算。请明确「{topic}」中研究对象、已知量、未知量与成立条件；再检查你的每个等式是否量纲一致。'
    if level == 2:
        extra = "你还没有记录自己的尝试，请先写出受力图或基本方程。" if not attempt else "从你写出的第一条基本方程开始，逐项标注来源和正方向。"
        return f"建议把问题拆成：建立模型 → 选择坐标/守恒量 → 写基本方程 → 检查边界条件。{extra}"
    return '请写出最小方程组，先求符号表达式，再代入数值。最后用量纲、特殊极限和数量级做三重检查；把仍卡住的具体一步补充到「我的尝试」中。'
