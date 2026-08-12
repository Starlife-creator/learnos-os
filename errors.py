"""错因模型（A3）：受控枚举 + 结构化画像字段。"""
from __future__ import annotations

from typing import Any

ERROR_TYPES: list[str] = [
    "concept_misunderstood",  # 概念理解错误
    "calculation",            # 计算错误
    "careless",               # 粗心/笔误
    "time_pressure",          # 时间压力下失误
    "misread",                # 审题错误
    "blank_in_facts",         # 事实/公式空白
    "heuristic_trap",         # 直觉陷阱
]

ERROR_TYPE_LABELS: dict[str, str] = {
    "concept_misunderstood": "概念理解错误",
    "calculation": "计算错误",
    "careless": "粗心笔误",
    "time_pressure": "时间压力",
    "misread": "审题错误",
    "blank_in_facts": "公式/事实空白",
    "heuristic_trap": "直觉陷阱",
}

# 旧版自由文本 → 枚举的归并映射（迁移时使用，保留原文本可追溯）
_ERROR_TYPE_MIGRATION: dict[str, str] = {
    "概念不清": "concept_misunderstood",
    "概念理解错误": "concept_misunderstood",
    "不会": "concept_misunderstood",
    "计算错误": "calculation",
    "算错": "calculation",
    "粗心": "careless",
    "马虎": "careless",
    "笔误": "careless",
    "审题不清": "misread",
    "看错题": "misread",
    "公式忘了": "blank_in_facts",
    "公式不熟": "blank_in_facts",
    "记不住公式": "blank_in_facts",
    "陷阱": "heuristic_trap",
    "直觉陷阱": "heuristic_trap",
    "时间不够": "time_pressure",
    "超时": "time_pressure",
}


def normalize_error_type(value: Any) -> str:
    """归一化错因：枚举直接通过；已知自由文本归并；未知 → 待诊断。"""
    raw = str(value or "").strip()
    if not raw:
        return "待诊断"
    if raw in ERROR_TYPES:
        return raw
    return _ERROR_TYPE_MIGRATION.get(raw, "待诊断")


def is_valid_error_type(value: Any) -> bool:
    return value in ERROR_TYPES or value == "待诊断"
