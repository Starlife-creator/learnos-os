"""AI 降级层：未配置/断网时的本地启发式提示与产出。

由原 ai.py 拆出（ADR 0008）。零网络依赖，保证离线可用。
"""
from __future__ import annotations

import re
from typing import Any

from ai_prompts import _resolve_subject


def fallback_hint(problem: dict[str, Any], level: int, lang: str = "zh") -> str:
    """离线兜底提示：AI 不可用时的降级路径（本地无密钥默认模式）。

    按学科取提示：物理或未知学科回落原物理措辞（保持既有测试与物理流程不变）；
    其他学科用中性通用提示，不再出现「物理模型 / 量纲 / 受力图」等专属词。
    """
    topic = problem.get("topic") or ("这个问题" if lang == "zh" else "this problem")
    attempt = problem.get("my_attempt", "").strip()
    course = (problem.get("course") or "").strip()
    sbj = _resolve_subject(problem.get("subject", ""), course, topic)

    # 物理 / 未知学科：保留原措辞（向后兼容既有物理测试与流程）
    if sbj in ("physics", ""):
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

    # 非物理学科：中性通用提示（不出现物理专属词）
    if lang == "en":
        extra = ("You have not recorded your attempt yet — first outline the main steps or write down the key relations."
                 if not attempt else "Start from the first step you wrote and label each term's source.")
        if level == 1:
            return (f"Do not calculate yet. Clarify the object of study, the known and unknown quantities, and the "
                    f"conditions or assumptions for \"{topic}\"; then check that each step is self-consistent.")
        if level == 2:
            return (f"Break the problem into: understand the statement → list knowns/unknowns → "
                    f"establish the key relations → check the premises. {extra}")
        if level == 3:
            return ("Write the minimal set of solution steps, solve symbolically first if possible, then plug in numbers. "
                    "Do a check with special cases, limits and orders of magnitude; add the exact step "
                    "where you get stuck to \"My Attempt\".")
        return (f"Full solution framework: 1) identify the object and known/unknown quantities; "
                f"2) choose the applicable model or method ({topic}) and state its premises; "
                "3) write the key relations and check them line by line; "
                "4) solve symbolically first, then substitute numbers; "
                "5) verify with special cases and orders of magnitude. "
                "Compare key results with a standard solution — redo it yourself before checking.")

    # 非物理 · 中文
    if level == 1:
        return f'先不要计算。请明确「{topic}」的研究对象、已知量与未知量，以及需要注意的前提或适用条件；再检查每一步推导是否自洽、是否遗漏关键量。'
    if level == 2:
        extra = "你还没有记录自己的尝试，请先梳理题意、列出已知与未知。" if not attempt else "从你写出的第一步开始，逐项标注依据与来源。"
        return f"建议围绕「{topic}」把问题拆成：理解题意 → 列出已知/未知 → 建立关键关系 → 检查前提条件。{extra}"
    if level == 3:
        return '请写出最精简的求解步骤，先求符号或通式再代入具体值。最后用特例、极限或数量级做检查；把仍卡住的具体一步补充到「我的尝试」中。'
    return (f'完整求解框架：1) 明确研究对象与已知/未知量；2) 选择适用的模型或方法（{topic}）并写清前提；'
            '3) 列关键关系并逐项检查；4) 先求符号表达式再代数值；'
            '5) 用特例或数量级复核。'
            '关键结果与标准解析对照——建议先自己重做一遍再对答案。')
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
def local_tags(title: str, content: str, course: str = "", topic: str = "", subject: str = "") -> dict[str, Any]:
    """B5 降级：关键词规则打标签（无 AI 时可用）。置信度按匹配强度估算。

    物理或未知学科沿用原物理分科词库；其他学科仅用通用题型/课程/知识点标签，
    不再套用物理分科关键词（避免化学/数学/历史题被打上「电磁学」等物理标签）。
    """
    sbj = _resolve_subject(subject, course, topic)
    text = f"{title}\n{content}"
    found: list[str] = []
    hits = 0
    if course:
        found.append(f"课程:{course}")
        hits += 1
    if topic:
        found.append(f"知识点:{topic}")
        hits += 1
    if sbj in ("physics", ""):
        for label, words in _KNOWLEDGE_KEYWORDS.items():
            count = sum(text.count(w) for w in words)
            if count:
                found.append(f"知识点:{label}")
                hits += count
        for label, words in _METHOD_KEYWORDS.items():
            if any(w in text for w in words):
                found.append(f"方法:{label}")
                hits += 1
    for label, words in _TYPE_KEYWORDS.items():
        if any(w in text for w in words):
            found.append(f"题型:{label}")
            hits += 1
    if not found:
        found.append("知识点:待分类")
    confidence = min(0.95, 0.45 + 0.08 * hits) if hits > 0 else 0.3
    return {"tags": found, "confidence": round(confidence, 2), "source": "local"}
def local_variants(problem: dict[str, Any], subject: str = "") -> list[dict[str, Any]]:
    """A4 降级：离线参数化变式模板（按错因×题型启发式，零依赖）。"""
    content = problem.get("content", "")
    title = problem.get("title", "")
    topic = problem.get("topic", "")
    error_type = problem.get("error_type", "")
    sbj = _resolve_subject(subject, problem.get("course", ""), topic)
    is_physics = sbj == "physics"
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
        dim_note = "量纲与数量级" if is_physics else "单位与数量级"
        variants.append({
            "mode": "数值替换",
            "title": f"{title}（数值变式）",
            "content": replaced,
            "answer": f"同原题解法，注意代入新数值后重新检查{dim_note}。",
        })
    # 情境替换：换一个相近场景（概念/建模错）
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
                "answer": "模型不变，注意新场景下边界条件与关键量的差异。",
            })
            break
    # 反向设问：把"求 X"改为"给 X 反推条件"（概念错场景）
    m = re.search(r"(?:求|计算|大小为|等于)\s*([^，。；]+)", content)
    if m:
        target = m.group(1).strip()
        dim_verify = "验证量纲" if is_physics else "验证单位与数量级"
        variants.append({
            "mode": "反向设问",
            "title": f"{title}（反向设问）",
            "content": f"给定结果 {target} = 已知值，反推题目中某一初始条件；写出推导过程并{dim_verify}。",
            "answer": "把原题正向关系反解为初始条件表达式，代入结果校验。",
        })
    if not variants:
        variants.append({
            "mode": "重述练习",
            "title": f"{title}（重述）",
            "content": f"不看书本，用自己的话重新表述 {topic or title} 的解题思路，并写出关键关系及适用条件。",
            "answer": "对照标准解析检查：模型选择、关系适用条件、边界条件是否完整。",
        })
    return variants
def local_bank_question(subject: str, topic: str, qtype: str) -> dict[str, Any]:
    """离线降级：产出一道开放式自评占位题（零依赖）。source=manual：非 AI 生成不冒充。"""
    return {
        "type": "single",
        "stem": f"【{topic or '知识点'}】请用自己的话简述其核心要点并举例说明。",
        "choices": ["能正确表述并举例", "部分正确", "概念混淆", "完全错误"],
        "answer": 0,
        "explain": "离线模式仅提供开放式自评占位，建议联网后重新生成。",
        "source": "manual",
    }
