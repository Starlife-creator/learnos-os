"""AI 提示词层：学科档案、阶段提示词、结构化输出 schema。

由原 ai.py 拆出（ADR 0008）。只含常量与纯函数，不做网络调用。
"""
from __future__ import annotations

import re
from typing import Any

from config import LOG




# ─────────────────────────────────────────────────────────────
# 学科感知导师人格（subject-aware tutor personas）  [v1]
# 原硬编码的"物理"人格改为按 subject 取模板；未知学科回落 generic（中性），
# 不再一律物理化。physics 档完整保留原始措辞，既有物理流程不变。
# ─────────────────────────────────────────────────────────────

_SUBJECT_DISPLAY = {
    "physics": "物理", "chemistry": "化学", "math": "数学", "biology": "生物",
    "history": "历史", "geography": "地理", "cs": "计算机", "programming": "编程",
    "english": "英语", "chinese": "语文", "default": "该学科",
}
_SUBJECT_ALIASES = {
    "大学物理": "physics", "物理": "physics", "电磁学": "physics", "力学": "physics",
    "热学": "physics", "光学": "physics", "原子物理": "physics",
    "电磁感应": "physics", "电磁场": "physics", "量子力学": "physics", "电动力学": "physics",
    "化学": "chemistry", "大学化学": "chemistry", "有机化学": "chemistry", "无机化学": "chemistry",
    "化学反应": "chemistry", "化学平衡": "chemistry",
    "数学": "math", "高等数学": "math", "线性代数": "math", "微积分": "math",
    "导数": "math", "概率论": "math", "离散数学": "math", "解析几何": "math",
    "生物": "biology", "生物学": "biology",
    "历史": "history", "世界史": "history",
    "地理": "geography",
    "计算机": "cs", "编程": "programming", "程序": "programming", "代码": "programming",
    "英语": "english",
    "语文": "chinese", "中文": "chinese",
}


def _subject_display(subject: str) -> str:
    return _SUBJECT_DISPLAY.get((subject or "").strip().lower(), _SUBJECT_DISPLAY["default"])


def _resolve_subject(*candidates: str) -> str:
    """从 subject / course / topic 候选中解析学科键；未知 → ''（回落 generic）。"""
    known = set(_SUBJECT_DISPLAY) - {"default"}
    for c in candidates:
        c = (c or "").strip().lower()
        if c in known:
            return c
        if c in _SUBJECT_ALIASES:
            return _SUBJECT_ALIASES[c]
    return ""


# 中性通用子组件（非物理学科复用，避免物理化措辞）
_GENERIC_STAGE_LABELS = {"concept": "核心概念", "premise": "前提条件", "counterexample": "反例辨析",
                         "extreme": "边界推演", "verify": "检验设计"}
_GENERIC_STAGE_PROMPTS = {
    "concept": '请先用直观的方式解释「{topic}」的核心含义，尽量少用术语。',
    "premise": "这个结论成立需要哪些前提？请至少说出两个。",
    "counterexample": "请给出一个容易误用该概念的反例，并解释错在哪里。",
    "extreme": "如果某个关键参数趋近于极端值，结果应该怎样变化？",
    "verify": "请设计一种方法来检验你刚才的解释。",
}
_GENERIC_DEEPER_PROMPTS = {
    "concept": "你的解释还停在表面。请换一个角度重新描述一次，只说核心过程，不要堆术语。",
    "premise": "前提说得不完整。请再补一个容易被忽略的适用条件，并说明违反它会出现什么错误结果。",
    "counterexample": "这个反例说服力不够。请换成「{topic}」最容易误用的一处边界情况，指出误用者错在哪一步。",
    "extreme": "请具体说明：参数趋于极端时哪个量变化最剧烈、哪个量趋于稳定，各有什么意义。",
    "verify": "请给出关键的观测/验证量、预期范围，以及怎样排除干扰。",
}
_GENERIC_WEAK_PATTERNS = [
    (re.compile(r"适用条件|前提|假设"), "适用条件/前提交代不全"),
    (re.compile(r"公式|代入|计算|推导"), "推导与计算不够严谨"),
    (re.compile(r"反例|误用|边界"), "概念边界与反例辨析不足"),
    (re.compile(r"极限|边界|趋于|极端"), "边界/极端情形分析不完整"),
    (re.compile(r"验证|检验|实验|测量"), "检验/验证设计思路不足"),
]
_GENERIC_ERR_CHECK = {
    "概念理解错误": "先帮学生核对核心概念与适用条件，指出概念误区，不直接给答案。",
    "计算错误": "请复核运算过程，指出第几步可能出错并引导重算。",
    "粗心笔误": "提示按步骤检查符号、单位和抄写，提醒这类失误最容易在细节处。",
    "时间压力": "提醒先建立最少必要步骤的解题顺序，并给出抢分优先级建议。",
    "审题错误": "指出题目中容易被忽略的条件和关键词，引导重新读题。",
    "公式/事实空白": "给出关键公式或事实的适用边界与推导线索，帮助回忆而非背诵。",
    "直觉陷阱": "提示先用特例或极限检验直觉，指出反直觉点，不要直接判对错。",
}

# 物理档（BASE）：完整保留原始措辞，保证既有物理流程不变
_BASE_PROFILE: dict[str, Any] = {
    "ta_zh": "你是严格而耐心的大学物理助教。你的任务是促进主动学习，不是替学生交作业。"
             "优先检查物理模型、适用条件、量纲、边界条件和极限情况。使用中文和清晰的 LaTeX。",
    "ta_en": "You are a strict but patient university physics TA. Your job is to promote active "
             "learning, not to do the homework for the student. "
             "Prioritize checking the physical model, applicable conditions, dimensions, "
             "boundary conditions and limiting cases. Answer in English with clear LaTeX.",
    "tag_extractor": "你是物理题标签提取器。根据题目提取 3-6 个标签，每项格式必须是 "
                     "'知识点:名称'、'题型:名称'、'难度:易|中|难'、'方法:名称'、'错因:名称' 之一。"
                     "只返回 JSON，不要多余文字。结构为 {tags: [...], confidence: 0-1}。\n"
                     "参考示例：题目：求斜面上物体沿 30° 角下滑的加速度。"
                     "输出：{\"tags\": [\"知识点:斜面受力分析\", \"题型:计算\", \"难度:中\", "
                     "\"方法:受力分解\", \"错因:摩擦力方向\"], \"confidence\": 0.92}",
    "variant_author": "你是物理出题助手。基于给定错题生成 3 道变式，三题模式分别为：数值替换、情境替换、反向设问。"
                      "每题必须包含 mode、title、content、answer 四个字段，只返回 JSON。可选补充 type（single/multiple/fill/subjective/composite，缺省开放式 subjective）与 choices（选择题选项数组）；选择题 answer 填正确选项字母。",
    "feynman_novice": "向一位完全不懂物理的新手讲解。规则：不许读公式，只讲物理图像与直觉。"
                      "讲完直接发送，我会帮你对照标准解析找漏点。",
    "oral_teacher": "你是严格的大学物理口试老师（苏格拉底式）。",
    "oral_teacher_summary": "你是严格的大学物理口试老师。这是最后一轮总结。",
    "oral_card_author": "你是物理老师。基于口试记录生成一道复习题的题目正文（一段话），"
                        "围绕薄弱点设计，要求学生用自己的话重述概念并辨析反例。只输出题目正文，不要其他内容。",
    "stage_labels": {"concept": "物理图像", "premise": "前提条件", "counterexample": "反例辨析",
                     "extreme": "极限推演", "verify": "检验设计"},
    "stage_prompts": {
        "concept": '请不用公式，先用物理图像解释「{topic}」的核心含义。',
        "premise": "这个结论成立需要哪些前提？请至少说出两个。",
        "counterexample": "请给出一个容易误用该概念的反例，并解释错在哪里。",
        "extreme": "如果某个关键参数趋近于零或无穷大，结果应该怎样变化？",
        "verify": "请设计一种实验或数值方法来检验你刚才的解释。",
    },
    "deeper_prompts": {
        "concept": "你的解释还停在表面。请从力的来源或能量角度重新描述一次，不许用公式，只说物理过程。",
        "premise": "前提说得不完整。请再补一个容易被忽略的适用条件，并说明违反它会出现什么错误结果。",
        "counterexample": "这个反例说服力不够。请换成「{topic}」最容易误用的一处边界情况，指出误用者错在哪一步。",
        "extreme": "请具体说明：参数趋于极限时哪个量发散、哪个量饱和，各有什么物理意义。",
        "verify": "请给出实验的关键观测量、预期数值范围，以及怎样排除系统误差。",
    },
    "weak_patterns": [
        (re.compile(r"适用条件|前提|假设"), "适用条件/前提交代不全"),
        (re.compile(r"公式|代入|计算"), "公式推导与代入计算不够严谨"),
        (re.compile(r"反例|误用|边界"), "概念边界与反例辨析不足"),
        (re.compile(r"量纲|极限|趋于"), "量纲/极限分析不完整"),
        (re.compile(r"实验|验证|测量"), "检验/实验设计思路不足"),
    ],
    "err_check": {
        "概念理解错误": "先帮学生核对物理模型与适用条件，指出概念误区，不直接给公式。",
        "计算错误": "请复核运算过程与量纲，指出第几步可能出错并引导重算。",
        "粗心笔误": "提示按步骤检查符号、单位和抄写，提醒这类失误最容易在符号正负号。",
        "时间压力": "提醒先建立最少必要步骤的解题顺序，并给出抢分优先级建议。",
        "审题错误": "指出题目中容易被忽略的条件和关键词，引导重新读题。",
        "公式/事实空白": "给出提示公式的适用边界与推导线索，帮助回忆而非背诵。",
        "直觉陷阱": "提示先用特例或极限检验直觉，指出反直觉点，不要直接判对错。",
    },
    "card_local": "请重新解释该概念的物理图像、适用前提，并给出一个易误用反例。",
}

# 化学档
_CHEM_PROFILE: dict[str, Any] = {
    "ta_zh": "你是严格而耐心的化学助教。你的任务是促进主动学习，不是替学生交作业。"
             "优先检查反应原理、物质结构与性质、方程式配平、条件控制与定量关系。使用中文，必要时用清晰的公式。",
    "ta_en": "You are a strict but patient chemistry tutor. Your job is to promote active learning, "
             "not to do the homework for the student. Prioritize reaction principles, structure and "
             "properties, equation balancing, conditions and stoichiometry. Answer in English.",
    "tag_extractor": "你是化学题标签提取器。根据题目提取 3-6 个标签，每项格式必须是 "
                     "'知识点:名称'、'题型:名称'、'难度:易|中|难'、'方法:名称'、'错因:名称' 之一。"
                     "只返回 JSON，不要多余文字。结构为 {tags: [...], confidence: 0-1}。\n"
                     "参考示例：题目：配平 Fe + O₂ → Fe₂O₃ 并计算电子转移。"
                     "输出：{\"tags\": [\"知识点:氧化还原\", \"题型:配平\", \"难度:中\", "
                     "\"方法:电子守恒\", \"错因:化合价判断\"], \"confidence\": 0.9}",
    "variant_author": "你是化学出题助手。基于给定错题生成 3 道变式，三题模式分别为：数值替换、情境替换、反向设问。"
                      "每题必须包含 mode、title、content、answer 四个字段，只返回 JSON。可选补充 type（single/multiple/fill/subjective/composite，缺省开放式 subjective）与 choices（选择题选项数组）；选择题 answer 填正确选项字母。",
    "feynman_novice": "向一位完全不懂化学的新手讲解。规则：不许堆砌术语，只讲核心直觉与图像。"
                      "讲完直接发送，我会帮你对照标准解析找漏点。",
    "oral_teacher": "你是严格的化学口试老师（苏格拉底式）。",
    "oral_teacher_summary": "你是严格的化学口试老师。这是最后一轮总结。",
    "oral_card_author": "你是化学老师。基于口试记录生成一道复习题的题目正文（一段话），"
                        "围绕薄弱点设计，要求学生用自己的话重述概念并辨析反例。只输出题目正文，不要其他内容。",
    "stage_labels": _GENERIC_STAGE_LABELS,
    "stage_prompts": _GENERIC_STAGE_PROMPTS,
    "deeper_prompts": _GENERIC_DEEPER_PROMPTS,
    "weak_patterns": _GENERIC_WEAK_PATTERNS,
    "err_check": _GENERIC_ERR_CHECK,
    "card_local": "请重新解释该概念的核心图像、适用前提，并给出一个易误用反例。",
}

# 数学档
_MATH_PROFILE: dict[str, Any] = {
    "ta_zh": "你是严格而耐心的数学助教。你的任务是促进主动学习，不是替学生交作业。"
             "优先检查定义、定理适用条件、逻辑推导、计算与边界情况。使用中文和清晰的 LaTeX。",
    "ta_en": "You are a strict but patient mathematics tutor. Your job is to promote active learning, "
             "not to do the homework for the student. Prioritize definitions, theorem conditions, "
             "logical derivation, computation and boundary cases. Answer in English with clear LaTeX.",
    "tag_extractor": "你是数学题标签提取器。根据题目提取 3-6 个标签，每项格式必须是 "
                     "'知识点:名称'、'题型:名称'、'难度:易|中|难'、'方法:名称'、'错因:名称' 之一。"
                     "只返回 JSON，不要多余文字。结构为 {tags: [...], confidence: 0-1}。\n"
                     "参考示例：题目：已知函数 f(x)=x³-3x，求其在区间 [-2,2] 上的最大值。"
                     "输出：{\"tags\": [\"知识点:导数应用\", \"题型:最值\", \"难度:中\", "
                     "\"方法:求导\", \"错因:端点遗漏\"], \"confidence\": 0.93}",
    "variant_author": "你是数学出题助手。基于给定错题生成 3 道变式，三题模式分别为：数值替换、情境替换、反向设问。"
                      "每题必须包含 mode、title、content、answer 四个字段，只返回 JSON。可选补充 type（single/multiple/fill/subjective/composite，缺省开放式 subjective）与 choices（选择题选项数组）；选择题 answer 填正确选项字母。",
    "feynman_novice": "向一位完全不懂数学的新手讲解。规则：不许堆砌术语，只讲核心直觉与图像。"
                      "讲完直接发送，我会帮你对照标准解析找漏点。",
    "oral_teacher": "你是严格的数学口试老师（苏格拉底式）。",
    "oral_teacher_summary": "你是严格的数学口试老师。这是最后一轮总结。",
    "oral_card_author": "你是数学老师。基于口试记录生成一道复习题的题目正文（一段话），"
                        "围绕薄弱点设计，要求学生用自己的话重述概念并辨析反例。只输出题目正文，不要其他内容。",
    "stage_labels": _GENERIC_STAGE_LABELS,
    "stage_prompts": _GENERIC_STAGE_PROMPTS,
    "deeper_prompts": _GENERIC_DEEPER_PROMPTS,
    "weak_patterns": _GENERIC_WEAK_PATTERNS,
    "err_check": _GENERIC_ERR_CHECK,
    "card_local": "请重新解释该定义或定理的核心直觉、适用前提，并给出一个易误用反例。",
}

# 中性通用档（未知学科回落）
_GENERIC_PROFILE: dict[str, Any] = {
    "ta_zh": "你是严格而耐心的学科助教。你的任务是促进主动学习，不是替学生交作业。"
             "优先检查核心概念、适用条件、逻辑与边界情况。使用中文，必要时用清晰的公式或 LaTeX。",
    "ta_en": "You are a strict but patient subject tutor. Your job is to promote active learning, "
             "not to do the homework for the student. Prioritize checking core concepts, applicable "
             "conditions, logic and boundary cases. Answer in English, using clear formulas or LaTeX when needed.",
    "tag_extractor": "你是题目标签提取器。根据题目提取 3-6 个标签，每项格式必须是 "
                     "'知识点:名称'、'题型:名称'、'难度:易|中|难'、'方法:名称'、'错因:名称' 之一。"
                     "只返回 JSON，不要多余文字。结构为 {tags: [...], confidence: 0-1}。\n"
                     "参考示例：题目：求 y=x² 在 x=1 处的导数。"
                     "输出：{\"tags\": [\"知识点:导数\", \"题型:计算\", \"难度:易\", \"方法:幂函数求导\"], "
                     "\"confidence\": 0.95}",
    "variant_author": "你是出题助手。基于给定错题生成 3 道变式，三题模式分别为：数值替换、情境替换、反向设问。"
                      "每题必须包含 mode、title、content、answer 四个字段，只返回 JSON。可选补充 type（single/multiple/fill/subjective/composite，缺省开放式 subjective）与 choices（选择题选项数组）；选择题 answer 填正确选项字母。",
    "feynman_novice": "向一位完全不懂这个主题的新手讲解。规则：不许堆砌术语，只讲核心直觉与图像。"
                      "讲完直接发送，我会帮你对照标准解析找漏点。",
    "oral_teacher": "你是严格的口试老师（苏格拉底式）。",
    "oral_teacher_summary": "你是严格的口试老师。这是最后一轮总结。",
    "oral_card_author": "你是老师。基于口试记录生成一道复习题的题目正文（一段话），"
                        "围绕薄弱点设计，要求学生用自己的话重述概念并辨析反例。只输出题目正文，不要其他内容。",
    "stage_labels": _GENERIC_STAGE_LABELS,
    "stage_prompts": _GENERIC_STAGE_PROMPTS,
    "deeper_prompts": _GENERIC_DEEPER_PROMPTS,
    "weak_patterns": _GENERIC_WEAK_PATTERNS,
    "err_check": _GENERIC_ERR_CHECK,
    "card_local": "请重新解释该概念的核心图像、适用前提，并给出一个易误用反例。",
}

_SUBJECT_OVERRIDES: dict[str, dict[str, Any]] = {
    "chemistry": _CHEM_PROFILE,
    "math": _MATH_PROFILE,
}


def _subject_profile(subject: str = "") -> dict[str, Any]:
    """返回学科感知导师人格；physics 原样、chem/math 专属、未知回落中性通用。"""
    key = _resolve_subject(subject)
    if not key or key == "physics":
        return _BASE_PROFILE
    override = _SUBJECT_OVERRIDES.get(key)
    if not override:
        return _GENERIC_PROFILE
    return {**_BASE_PROFILE, **override}


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
    p = _subject_profile(problem.get("subject", ""))
    # C6：按错因定向检查（提高提示针对性）
    error_line = ""
    try:
        from errors import ERROR_TYPE_LABELS, normalize_error_type
        et = normalize_error_type(problem.get("error_type"))
        if et in ERROR_TYPE_LABELS:
            error_line = f"（该学生标记的错因：{ERROR_TYPE_LABELS[et]}。针对性要求：{p['err_check'][ERROR_TYPE_LABELS[et]]}）"
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
                "content": p["ta_en"] + profile_line,
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
            "content": p["ta_zh"] + profile_line,
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


_TAG_SCHEMA = {
    "tags": {"type": "array", "items": {"type": "string"}, "required": True},
    "confidence": {"type": "number", "min": 0.0, "max": 1.0, "required": True},
}
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
                "type": {"type": "string", "enum": ["single", "multiple", "fill", "subjective", "composite"]},
                "choices": {"type": "array", "items": {"type": "string"}},
            },
        },
        "required": True,
    },
}
_BANK_Q_TYPES = ("single", "multiple", "fill", "subjective", "composite")

_BANK_Q_SCHEMA = {
    "question": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": list(_BANK_Q_TYPES), "required": True},
            "stem": {"type": "string", "min_length": 5, "required": True},
            "choices": {"type": "array", "items": {"type": "string"}},
            "answer": {"type": "any"},
            "explain": {"type": "string"},
            "parts": {"type": "array", "items": {"type": "object"}},
        },
        "required": True,
    },
}

_BANK_Q_PROMPT = (
    "你是学科题库出题助手。根据用户给的学科、知识点与题型，出 1 道高质量练习题，"
    "严格返回 JSON，结构为 {question: {...}}。字段：\n"
    "- type: 必填，单选=single / 多选=multiple / 填空=fill / 主观=subjective / 大小题=composite\n"
    "- stem: 题干（≥5 字）\n"
    "- choices: 仅 single/multiple 必填，选项数组（≥2 项）\n"
    "- answer: single=正确选项下标(从0)；multiple=正确下标数组；fill=答案字符串或数组(多空)；"
    "subjective=参考答案文本；composite 省略\n"
    "- explain: 解析\n"
    "- parts: 仅 composite 必填，子题数组，每个子题结构同 question（可递归嵌套）\n"
    "只返回 JSON，不要多余文字。\n"
    "参考示例（结构示范，学科与难度可改）：\n"
    '例1（单选）：{"question": {"type": "single", "stem": "两个带同种电荷的小球相互靠近时，作用力方向为？", '
    '"choices": ["互相吸引", "互相排斥", "无作用力", "引力"], "answer": 1, '
    '"explain": "同种电荷相斥。", "concept": "库仑定律"}}\n'
    '例2（大小题）：{"question": {"type": "composite", "stem": "一个物体从静止开始做匀加速直线运动，5s 内位移 25m。", '
    '"parts": [{"type": "single", "stem": "该物体的加速度为？", "choices": ["1 m/s^2", "2 m/s^2", "5 m/s^2", "10 m/s^2"], "answer": 1}, '
    '{"type": "fill", "stem": "第 5s 末的速度为 ____ m/s。", "answer": "10"}], '
    '"explain": "由 s=1/2at^2 得 a=2 m/s^2；v=at=10 m/s。"}}'
)
_REVIEW_SCHEMA = {
    "review": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["pass", "warn", "reject"], "required": True},
            "issues": {"type": "array", "items": {"type": "string"}},
            "comment": {"type": "string"},
            "revised": {"type": "string"},
        },
        "required": True,
    },
}

_REVIEW_PROMPT = (
    "你是严格的教学审题专家。对用户给出的 1 道题做质量审查，出具结论。\n"
    "审题维度（逐项检查后汇总）：\n"
    "1. 题干：是否清晰、无歧义、条件齐全、是否有遗漏或天生无法作答；\n"
    "2. 选择题：答案是否唯一/确定，错误项是否合理、是否有明显凑数；\n"
    "3. 填空/主观题：参考答案是否准确、表述是否严谨；\n"
    "4. 大小题(composite)：各子题是否相互独立、总分与问题是否匹配。\n"
    "只返回 JSON，结构为 {review: {...}}，字段：\n"
    "- verdict: pass(没问题) / warn(有小问题可改进) / reject(有硬伤必须改)\n"
    "- issues: 发现的问题数组（没问题也可给 1-2 条优化建议）\n"
    "- comment: 一句话总评\n"
    "- revised: 若 verdict=reject 给出建议修订后的完整题目 JSON 文本；否则可省略\n"
    "输出示例：{\"review\": {\"verdict\": \"warn\", \"issues\": [\"选项 B 表述有歧义，建议改为更明确的干扰项\"], "
    "\"comment\": \"题目整体合格，仅个别选项需打磨。\"}}"
)
_SCORE_SCHEMA = {
    "score": {
        "type": "object",
        "properties": {
            "score": {"type": "integer", "min": 0, "max": 100, "required": True},
            "comment": {"type": "string"},
            "against": {"type": "string"},
        },
        "required": True,
    },
}

_SCORE_PROMPT = (
    "你是严格、友好的学科阅卷老师。针对用户提供的【题目】【参考答案】【学生作答】，"
    "按 0-100 分给分并点评。\n"
    "评分方式：先对照下面的锚定档位表做匹配（把开放打分转成档位分类，保证一致性），"
    "再在档位区间内微调出具体分：\n"
    "- 90-100 分：全部要点准确覆盖，表述清晰，无实质错误；\n"
    "- 75-89 分：多数要点到位，个别表述不严谨或漏次要点；\n"
    "- 60-74 分：核心要点部分到位，有实质遗漏或关键表述混乱；\n"
    "- 40-59 分：只触及皮毛，多数要点缺失或明显误解；\n"
    "- 0-39 分：跑题、空白或严重错误（须在点评中说明原因并给改进方向）。\n"
    "其他要求：对照参考答案逐要点判分，不苛求措辞一致，抓关键点是否到位；"
    "comment 给出 1-3 句点评（中文）：先总评，再指出失分点与改进建议。\n"
    "只返回 JSON，结构为 {score: {score: 0-100 整数, comment: 点评, against: 命中要点简述}}。\n"
    "参考示例（结构示范）：\n"
    '输入：题目：简述牛顿第二定律。 参考答案：F=ma，F 与 a 同向。 学生：F 等于质量乘加速度。\n'
    '输出：{"score": {"score": 85, "comment": "要点基本到位，但漏了方向关系，建议补充 F 与 a 同向。", '
    '"against": "F=ma"}}\n'
    '输入：题目：简述牛顿第二定律。 参考答案：F=ma，F 与 a 同向。 学生：牛顿第二定律是力学基本定律。\n'
    '输出：{"score": {"score": 30, "comment": "仅复述了定律地位，未给出 F=ma 与方向关系，核心要点缺失。", '
    '"against": "无"}}'
)
