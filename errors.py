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
    "概念错误": "concept_misunderstood",
    "概念理解": "concept_misunderstood",
    "概念理解错误": "concept_misunderstood",
    "不会": "concept_misunderstood",
    "建模错误": "concept_misunderstood",
    "计算错误": "calculation",
    "算错": "calculation",
    "粗心": "careless",
    "马虎": "careless",
    "笔误": "careless",
    "粗心笔误": "careless",
    "符号/方向": "careless",
    "符号方向": "careless",
    "审题不清": "misread",
    "看错题": "misread",
    "审题错误": "misread",
    "公式忘了": "blank_in_facts",
    "公式不熟": "blank_in_facts",
    "记不住公式": "blank_in_facts",
    "公式/事实空白": "blank_in_facts",
    "陷阱": "heuristic_trap",
    "直觉陷阱": "heuristic_trap",
    "时间不够": "time_pressure",
    "超时": "time_pressure",
    "时间压力": "time_pressure",
}


def normalize_error_type(value: Any) -> str:
    """归一化错因：枚举直接通过；已知自由文本归并；未知 → 待诊断。"""
    raw = str(value or "").strip()
    if not raw:
        return "待诊断"
    if raw in ERROR_TYPES:
        return raw
    return _ERROR_TYPE_MIGRATION.get(raw, "待诊断")


# M2 队列错因权重（B3）：元认知类（公式/事实空白）与概念理解错最优先重考——
# 「不会」与「算错」区别对待；粗心/时间压力属执行层问题，最低优先。
# 仅用于复习队列排序（纯读，遵守 F1 出队零写入条款），不落库。
ERROR_TYPE_QUEUE_WEIGHTS: dict[str, int] = {
    "blank_in_facts": 3,        # 公式/事实空白：知识本身缺失
    "concept_misunderstood": 3, # 概念理解错误：知识本身错误
    "heuristic_trap": 2,        # 直觉陷阱：需要针对性纠正
    "misread": 1,               # 审题错误
    "calculation": 1,           # 计算错误
    "careless": 0,              # 粗心笔误：重考收益低
    "time_pressure": 0,         # 时间压力：非知识问题
}


def queue_weight(error_type: Any) -> int:
    """错因 → 复习队列优先权重（M2）。

    空白/概念错 3 > 陷阱 2 > 计算/审题 1 > 粗心/时间/待诊断 0。
    未知错因（含「待诊断」与历史自由文本）不加权，避免老数据整体前置。
    """
    return ERROR_TYPE_QUEUE_WEIGHTS.get(str(error_type or ""), 0)


def is_valid_error_type(value: Any) -> bool:
    return value in ERROR_TYPES or value == "待诊断"
