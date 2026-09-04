"""资料导入向导：教材/试卷文本 →（AI 提取，草稿不落库）概念图谱 / 题库例题 / 试卷结构。

R3 合规：analyze 只返回草稿，apply 按用户确认后的内容写入
（图谱 graph.add_concept + concept_links、题库 bank.import_questions、试卷 exam.create_paper）。
AI 不可用时对 md/txt 提供启发式降级：标题层级 → 章节概念（例题/试卷需 AI，无则明确报错）。
"""
from __future__ import annotations

import re
from typing import Any

from config import LOG
from db import DB_LOCK, db, now, rows
from validate import validate_object, SchemaError

# 分批参数：批大小按配置的 AI 上下文自适应（见 batch_chars），
# 批数不设硬上限（全文自动分段覆盖），仅设安全上限防失控账单。
MAX_BATCHES_HARD = 200


def batch_chars(context_tokens: int) -> int:
    """按模型上下文推算单批字符数。

    输入预算 = 上下文 × 45%（另一半留给输出、系统提示与安全余量）；
    中文文本约 1.1 字符/token；批大小夹在 [4k, 24k] 字符。
    例：8k 上下文 → 4k 字符/批；32k → 15k；128k → 24k（顶格）。
    """
    budget_tokens = max(2000, int(int(context_tokens) * 0.45))
    return max(4000, min(12000, int(budget_tokens * 1.1)))

_CONCEPT_SCHEMA = {
    "chapters": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string", "min_length": 1, "required": True},
    }}},
    "concepts": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string", "min_length": 1, "required": True},
        "chapter": {"type": "string"},
        "related": {"type": "array", "items": {"type": "string"}},
    }}},
}

# 与 bank.QUESTION_TYPES 对齐的多题型提取 schema（结构宽松校验，
# 语义校验统一交给 bank.import_questions 在 apply 时逐条执行）。
_Q_TYPES = ("single", "multiple", "fill", "subjective", "composite")
_QUESTION_SCHEMA = {
    "questions": {"type": "array", "items": {"type": "object", "properties": {
        "type": {"type": "string", "enum": list(_Q_TYPES), "required": True},
        "stem": {"type": "string", "required": True},
        "choices": {"type": "array", "items": {"type": "string"}},
        "answer": {"type": "any"},
        "explain": {"type": "string"},
        "concept": {"type": "string"},
        "unit": {"type": "string"},
        "difficulty": {"type": "integer", "min": 1, "max": 5},
        "parts": {"type": "array", "items": {"type": "object"}},
        "source": {"type": "string"},  # C3 来源标记（apply 时统一注入 material）
    }}},
}

_PAPER_SCHEMA = {
    "name": {"type": "string", "min_length": 1, "required": True},
    "questions": {"type": "array", "items": {"type": "object", "properties": {
        "qno": {"type": "string"},
        "topic": {"type": "string", "min_length": 1, "required": True},
        "content": {"type": "string"},
        "weight": {"type": "number", "min": 0.5, "max": 5},
    }}},
}


def _split_batches(text: str, batch_size: int) -> list[str]:
    """按段落边界切批（全文覆盖；超出 MAX_BATCHES_HARD 由 analyze 报错）。"""
    text = text.strip()
    if not text:
        return []
    paras = re.split(r"\n\s*\n", text)
    batches: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for p in paras:
        # 超长单段（无空行的大块）强制按批大小切片
        while len(p) > batch_size:
            if cur:
                batches.append("\n\n".join(cur))
                cur, cur_len = [], 0
            batches.append(p[:batch_size])
            p = p[batch_size:]
        if cur and cur_len + len(p) > batch_size:
            batches.append("\n\n".join(cur))
            cur, cur_len = [], 0
        cur.append(p)
        cur_len += len(p)
    if cur:
        batches.append("\n\n".join(cur))
    return batches


# ── 方案B：模型断句动态分段 ─────────────────────────────────────────────
# 静态切批的边界可能落在句子/段落中间。对每个边界，取"前批尾 OVERLAP 字 +
# 后批头 OVERLAP 字"拼成重叠片段，用 1 次微型模型调用让模型指出语义完整断点
# （输出该把重叠部分在哪一分，如"前1行后2行"或直接给在片段中的位置），
# 调整边界后每批语义完整 → 提取质量更高、更少截断。
_OVERLAP = 200          # 重叠窗口（字）；可按效果调 100~400
_SPLIT_PROMPT = (
    "下面是一段文本的重叠区（中间是上一段与下一段的衔接处，用「|||」标记）。"
    "请判断应从哪里切开，使两侧语义都完整（不切断句子/条目/小节）。"
    '只返回 JSON：{"side": "left"|"right", "reason": "一句话理由"}\n'
    "left=把重叠区归给左侧段（衔接处偏左），right=归给右侧段（衔接处偏右）。"
)


def _model_split_side(overlap_text: str) -> str | None:
    """调微型模型判断重叠区应归属左/右侧段。失败/离线返回 None（保守不调整）。"""
    try:
        from ai import call_ai, ai_configured
        if not ai_configured():
            return None
        from validate import validate_object
        raw = call_ai([
            {"role": "system", "content": _SPLIT_PROMPT},
            {"role": "user", "content": overlap_text},
        ], max_tokens=120, tier="fast", route="material", json_mode=True)
        data = validate_object(raw, {"side": {"type": "string", "required": True},
                                     "reason": {"type": "string"}})
        side = str(data.get("side", "")).strip().lower()
        return side if side in ("left", "right") else None
    except Exception:
        return None


def _refine_batch_boundaries(batches: list[str], batch_size: int, dry_run: bool = False) -> list[str]:
    """对静态批次边界做"模型断句"微调：重叠区交给模型判断归属，调整批次切分。

    实现：把每个相邻边界放入重叠上下文（前批尾 + 后批头 OVERLAP 字），
    模型输出 left/right；left=边界前移（后批多留），right=边界后移（前批多拿）。
    微调量限制在 [OVERLAP*0.3, OVERLAP] 内，避免批次长度失控。
    dry_run=True：不调模型、不调整（仅用于批数预估，避免重复计费）。
    """
    if len(batches) < 2:
        return batches
    if dry_run:
        return batches
    out: list[str] = []
    for i, b in enumerate(batches):
        if i == len(batches) - 1:
            out.append(b)
            continue
        nxt = batches[i + 1]
        tail = b[-_OVERLAP:] if len(b) >= _OVERLAP else b
        head = nxt[:_OVERLAP] if len(nxt) >= _OVERLAP else nxt
        overlap = f"{tail}\n|||\n{head}"
        side = _model_split_side(overlap)
        # 按模型建议微调边界（限制幅度防失控）
        move = int(_OVERLAP * 0.5)
        if side == "left":
            # 重叠区归左侧：把 head 的前移回 b（边界前移）——实际是 b 多收一个片段
            # 保守实现：边界向后批移 MAX_BATCH_ADJUST 个字，使衔接处落在后批开头完整语义内
            take = min(move, len(nxt) // 2)
            if take:
                out.append(b + nxt[:take].rstrip())
                batches[i + 1] = nxt[take:]
            else:
                out.append(b)
        elif side == "right":
            # 重叠区归右侧：前批让出尾部 move 字给后批（衔接偏右）
            give = min(move, len(b) // 4)
            if give:
                out.append(b[:-give].rstrip())
                batches[i + 1] = b[-give:] + nxt
            else:
                out.append(b)
        else:
            out.append(b)  # 模型不可用/失败 → 保持原边界
    return out


# 触发拆半重试的最小批长（低于此直接放弃，避免无限递归）
_SPLIT_MIN = 600


def _split_batch_half(batch_text: str) -> list[str]:
    """把一批文本按段落边界尽量对半切（找不到分界时按长度中点切）。"""
    half = len(batch_text) // 2
    lines = batch_text.split("\n")
    # 找最接近中点的段落边界
    best = -1
    best_d = len(batch_text)
    acc = 0
    for idx, ln in enumerate(lines):
        acc += len(ln) + 1
        if abs(acc - half) < best_d:
            best_d = abs(acc - half)
            best = idx + 1
    if best > 0 and best < len(lines):
        return ["\n".join(lines[:best]).strip(), "\n".join(lines[best:]).strip()]
    # 无段落边界（单大段）→ 按字符中点硬切
    return [batch_text[:half].strip(), batch_text[half:].strip()]


def _clean_concepts(batch_text: str, _depth: int = 0) -> dict[str, Any] | None:
    from ai import call_ai
    from validate import SchemaError as _SchemaError
    prompt = [
        {"role": "system", "content": (
            "你是知识图谱编辑。从给定教材片段中提取章节与核心概念，不要解释，严格只输出 JSON 对象："
            '{"chapters": [{"name": "章节名"}], "concepts": [{"name": "概念名", '
            '"chapter": "所属章节名", "related": ["相关概念名"]}]}。'
            "要求：概念必须是学科概念（不要页码/题号/人名）；related 只填同片段出现过的概念；没有则给空数组。"
        )},
        {"role": "user", "content": batch_text},
    ]
    raw_out = call_ai(prompt, max_tokens=4000, tier="heavy", route="material", json_mode=True)
    try:
        data = validate_object(raw_out, _CONCEPT_SCHEMA)
    except _SchemaError as exc:
        # 解析/校验失败（可能因输出被 max_tokens 截断，含"修复后值缺失"变形）→
        # 拆半递归重试（内容不丢），深度≤2 防无限递归；拆半后仍失败才放弃该批
        if len(batch_text) > _SPLIT_MIN and _depth < 2:
            return _clean_concepts_split(batch_text, _depth)
        LOG.warning("概念提取解析失败，模型原始返回[:300]=%r", str(raw_out)[:300])
        raise
    if not data["chapters"] and not data["concepts"]:
        return None
    return data


def _clean_concepts_split(batch_text: str, _depth: int) -> dict[str, Any] | None:
    """批拆两半递归提取并合并结果（截断自愈：减小单次输出需求）。"""
    from validate import SchemaError as _SchemaError
    halves = [h for h in _split_batch_half(batch_text) if h]
    if not halves:
        raise ValueError("批次拆分后为空")
    acc: dict[str, Any] = {"chapters": [], "concepts": []}
    ok = 0
    for h in halves:
        try:
            part = _clean_concepts(h, _depth + 1)
            if part:
                ok += 1
                _merge_concepts(acc, part)
        except (ValueError, _SchemaError) as exc_:
            LOG.warning("概念提取拆半后仍失败（跳过该半批）: %s", exc_)
    if not ok:
        raise ValueError("概念提取拆分后全部子批失败")
    return acc


_Q_EXTRACT_PROMPT = (
    "你是题库编辑。从给定教材/试卷片段中提取练习题，不要解释，严格只输出 JSON 对象："
    '{"questions": [{...}, ...]}。每题字段：\n'
    "- type: 必填，单选=single / 多选=multiple / 填空=fill / 主观=subjective / 大小题=composite\n"
    "- stem: 题干（大小题为材料/引导语）\n"
    "- choices: 仅 single/multiple 需要，选项数组（≥2 项）\n"
    "- answer: single=正确选项下标(从0)；multiple=正确下标数组；fill=答案字符串或数组(多空)；"
    "subjective=参考答案文本；composite 省略\n"
    "- explain: 解析\n"
    "- parts: 仅 composite 必填，子题数组，每个子题结构同本题（可嵌套），子题不再带 parts 之外的冗余字段\n"
    "- concept/unit/difficulty: 考查概念 / 所属单元 / 难度(1-5)\n"
    "规则：按题目在原文中的真实形态提取（单选给单选、多选给多选、填空给填空、解答/证明/论述给主观），"
    "不要强行改造成选择题；同一材料带多问的用 composite 归组；没有可提取的题目就返回空数组；"
    "不要自编题目或答案不确定的题。\n"
    "参考示例（结构示范）：\n"
    '{"questions": ['
    '{"type": "single", "stem": "下列说法正确的是", "choices": ["力是维持运动的原因", '
    '"惯性与速度有关", "质量是惯性大小的量度", "牛顿第二定律只适用于低速宏观"], "answer": 2, '
    '"explain": "质量是惯性大小的唯一量度。", "concept": "惯性", "unit": "力学", "difficulty": 2}, '
    '{"type": "composite", "stem": "一个物体从静止开始做匀加速直线运动，5s 内位移 25m。", '
    '"parts": [{"type": "single", "stem": "该物体的加速度为？", "choices": ["1 m/s^2", "2 m/s^2", '
    '"5 m/s^2", "10 m/s^2"], "answer": 1}, {"type": "fill", "stem": "第 5s 末的速度为 ____ m/s。", '
    '"answer": "10"}], "explain": "由 s=1/2at^2 得 a=2 m/s^2；v=at=10 m/s。", '
    '"concept": "匀加速直线运动", "unit": "力学", "difficulty": 3}]}'
)


def _norm_extracted_question(q: dict[str, Any]) -> dict[str, Any]:
    """把模型返回的一题裁剪为 bank.import_questions 可接受的多题型结构（composite 递归）。"""
    qtype = str(q.get("type") or "single").strip().lower()
    if qtype not in _Q_TYPES:
        qtype = "single"
    out: dict[str, Any] = {
        "type": qtype,
        "stem": str(q.get("stem") or "").strip()[:2000],
        "explain": str(q.get("explain") or "").strip()[:2000],
    }
    for k in ("concept", "unit"):
        v = str(q.get(k) or "").strip()
        if v:
            out[k] = v
    if q.get("difficulty"):
        out["difficulty"] = q["difficulty"]
    if qtype in ("single", "multiple") and isinstance(q.get("choices"), list):
        out["choices"] = [str(c).strip()[:200] for c in q["choices"]]
        out["answer"] = q.get("answer")
    elif qtype in ("fill", "subjective"):
        out["answer"] = q.get("answer")
    elif qtype == "composite":
        parts = q.get("parts")
        out["parts"] = [_norm_extracted_question(p) for p in (parts or [])
                        if isinstance(p, dict)]
        if not out["parts"]:
            raise ValueError("composite 缺少有效子题")
    return out


def _clean_questions(batch_text: str) -> list[dict[str, Any]] | None:
    from ai import call_ai
    prompt = [
        {"role": "system", "content": _Q_EXTRACT_PROMPT},
        {"role": "user", "content": batch_text},
    ]
    data = validate_object(call_ai(prompt, max_tokens=4000, tier="heavy", route="material",
                                   json_mode=True),
                           _QUESTION_SCHEMA)
    out: list[dict[str, Any]] = []
    for q in data["questions"] or []:
        try:
            out.append(_norm_extracted_question(q))
        except ValueError as exc:
            LOG.warning("提取例题单题归一化失败已跳过: %s", exc)
    return out or None


def _clean_paper(batch_text: str, first: bool) -> dict[str, Any] | None:
    from ai import call_ai
    name_rule = '"name": "试卷名称（从卷头识别）", ' if first else '"name": "", '
    prompt = [
        {"role": "system", "content": (
            "你是试卷录入员。判断给定片段是否为试卷，若是则提取题目清单，不要解释，严格只输出 JSON 对象："
            "{" + name_rule + '"questions": [{"qno": "题号", "topic": "考查知识点", '
            '"content": "题干摘要", "weight": 1}]}。'
            "weight 为分值/5（无分值则 1）。topic 必填。不是试卷或没有题目时 questions 给空数组。"
        )},
        {"role": "user", "content": batch_text},
    ]
    data = validate_object(call_ai(prompt, max_tokens=4000, tier="heavy", route="material",
                                   json_mode=True),
                           _PAPER_SCHEMA)
    if not data["questions"]:
        return None
    return data


def _heuristic_concepts(text: str) -> dict[str, Any]:
    """无 AI 降级：md/txt 标题层级 → 章节与概念草稿。"""
    chapters: list[dict[str, str]] = []
    concepts: list[dict[str, Any]] = []
    chapter = ""
    for line in text.splitlines():
        m = re.match(r"^(#{1,3})\s+(.+)$", line.strip()) or re.match(
            r"^第[一二三四五六七八九十百\d]+[章节讲]\s*(.*)$", line.strip())
        if not m:
            continue
        title = m.group(2).strip() if m.lastindex and m.lastindex >= 2 else m.group(0).strip()
        title = re.sub(r"[#*`]", "", title).strip()[:40]
        if not title:
            continue
        level = len(m.group(1)) if m.group(1).startswith("#") else 1
        if level == 1:
            chapter = title
            chapters.append({"name": title})
        else:
            concepts.append({"name": title, "chapter": chapter, "related": []})
    return {"chapters": chapters, "concepts": concepts}


def _merge_concepts(acc: dict[str, Any], part: dict[str, Any]) -> None:
    seen_ch = {c["name"] for c in acc["chapters"]}
    for ch in part.get("chapters", []):
        name = str(ch["name"]).strip()[:40]
        if name and name not in seen_ch:
            seen_ch.add(name)
            acc["chapters"].append({"name": name})
    seen_cp = {c["name"] for c in acc["concepts"]}
    for cp in part.get("concepts", []):
        name = str(cp["name"]).strip()[:40]
        if not name:
            continue
        rels = [str(r).strip()[:40] for r in (cp.get("related") or [])
                if str(r).strip() and str(r).strip() != name]
        if name in seen_cp:
            # M4：同名概念跨批出现 → related 取并集（原实现直接丢弃后续批次的边）
            existing = next(c for c in acc["concepts"] if c["name"] == name)
            for r in rels:
                if r not in existing["related"]:
                    existing["related"].append(r)
            if not existing.get("chapter") and cp.get("chapter"):
                existing["chapter"] = str(cp["chapter"]).strip()[:40]
            continue
        seen_cp.add(name)
        acc["concepts"].append({
            "name": name,
            "chapter": str(cp.get("chapter", "")).strip()[:40],
            "related": rels,
        })


# ── M4 跨片段建边第二遍（glossary 式两遍法）──────────────────
# 第一遍逐批抽概念时 related 锁定同片段（防模型编造清单外名字）；
# 第二遍把全量概念清单交给模型，只做"清单内配对"，补出跨批次/跨章节的边。
_RELATION_SCHEMA = {
    "concepts": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "min_length": 1, "required": True},
                "related": {"type": "array", "items": {"type": "string"}},
            },
        },
        "required": True,
    },
}

_RELATION_PROMPT = (
    "你是知识图谱编辑。下面是同一本教材中已提取的全部概念清单（按章节分组）。"
    "请找出清单内跨条目/跨章节的概念关联（前提-后继、直觉类比、易混对比等），"
    '只输出 JSON：{"concepts": [{"name": "概念名", "related": ["关联概念名", ...]}]}。'
    "要求：name 与 related 中出现的名字必须逐字取自清单；"
    "只列有实质学科关联的对，不要为凑数硬连；没有就返回空数组。"
)


def complete_relations(acc: dict[str, Any]) -> int:
    """第二遍补边：全量概念名清单 → 模型只做清单内配对 → 合并进 acc.related。

    返回新增边数。AI 失败/校验失败向上抛异常，由调用方降级为 warning（不阻断）。
    """
    from ai import call_ai
    names = {c["name"] for c in acc["concepts"]}
    if len(names) < 2:
        return 0
    by_ch: dict[str, list[str]] = {}
    for c in acc["concepts"]:
        by_ch.setdefault(c.get("chapter") or "未分组", []).append(c["name"])
    listing = "\n".join(f"{ch}：{'、'.join(ns[:20])}"
                        for ch, ns in list(by_ch.items())[:60])
    prompt = [
        {"role": "system", "content": _RELATION_PROMPT},
        {"role": "user", "content": f"概念清单：\n{listing}"},
    ]
    raw = call_ai(prompt, max_tokens=2000, tier="heavy", retries=1, route="material",
                  json_mode=True)
    data = validate_object(raw, _RELATION_SCHEMA)
    known = {c["name"]: c for c in acc["concepts"]}
    added = 0
    for item in data.get("concepts", []):
        node = known.get(str(item.get("name", "")).strip()[:40])
        if not node:
            continue
        for rel in item.get("related") or []:
            rel_name = str(rel).strip()[:40]
            if rel_name in names and rel_name != node["name"] \
                    and rel_name not in node["related"]:
                node["related"].append(rel_name)
                added += 1
    return added


def analyze(text: str, subject: str, targets: list[str],
            context_tokens: int = 32000, from_batch: int = 0,
            max_batches: int | None = None,
            progress=None) -> dict[str, Any]:
    """提取草稿（不落库）。批大小按上下文自适应；支持断点续跑与进度回调。

    from_batch/max_batches：本次只分析 [from_batch, from_batch+max_batches) 批，
    前端跨多次调用累积草稿（配合「继续分析」），已分析前缀不再重复计费。
    progress(info_dict)：每次 AI 调用完成时回调 {done, total, target}。
    返回 {draft, source, from_batch, to_batch, batches_total, batch_chars, ai_calls, warnings}。
    """
    targets = [t for t in targets if t in ("concepts", "questions", "paper")]
    if not targets:
        raise ValueError("请至少选择一种提取目标")
    size = batch_chars(context_tokens)
    batches_all = _split_batches(text, size)
    if not batches_all:
        raise ValueError("未提取到文本内容")
    # 方案B：模型断句微调批次边界（每边界 1 次微型调用，失败自动回退原边界）
    batches_all = _refine_batch_boundaries(batches_all, size)
    if len(batches_all) > MAX_BATCHES_HARD:
        raise ValueError(
            f"文本过长（需 {len(batches_all)} 批，超过安全上限 {MAX_BATCHES_HARD}），请拆分文件")
    from_batch = max(0, min(from_batch, len(batches_all)))
    end = len(batches_all) if max_batches is None else min(len(batches_all), from_batch + max(0, max_batches))
    batches = batches_all[from_batch:end]
    total_calls = len(batches) * len(targets)
    done_calls = 0

    def _tick(target: str) -> None:
        nonlocal done_calls
        done_calls += 1
        if progress:
            try:
                progress({"done": done_calls, "total": total_calls, "target": target,
                          "batch": from_batch + (done_calls - 1) // len(targets) + 1})
            except Exception:
                pass

    draft: dict[str, Any] = {}
    warnings: list[str] = []
    source = "ai"
    # 逐批容错：单批 AI 失败仅跳过该批（记录 warning），不整体降级；
    # 仅当某目标的所有批都失败时才对其回退启发式（仅 concepts 支持）。
    ok_counts = {t: 0 for t in targets}
    try:
        for target in targets:
            if target == "concepts":
                acc: dict[str, Any] = {"chapters": [], "concepts": []}
                for b in batches:
                    try:
                        part = _clean_concepts(b)
                        if part:
                            ok_counts[target] += 1
                            _merge_concepts(acc, part)
                    except (ValueError, SchemaError) as exc:
                        warnings.append(f"概念提取某批失败已跳过（{exc}）")
                    _tick("concepts")
                draft["concepts"] = acc
                # M4 跨批建边第二遍：仅多批且 AI 提取有产出时执行（单批无跨片段可连）
                if len(batches) >= 2 and ok_counts[target] > 0:
                    try:
                        added = complete_relations(acc)
                        if added:
                            warnings.append(f"跨片段概念关联已补充 {added} 条。")
                    except Exception as exc_:
                        warnings.append(f"跨片段关联补充失败已跳过：{exc_}")
            elif target == "questions":
                qs: list[dict[str, Any]] = []
                seen_stem: set[str] = set()
                for b in batches:
                    try:
                        part = _clean_questions(b)
                    except (ValueError, SchemaError) as exc:
                        warnings.append(f"例题提取某批失败已跳过（{exc}）")
                        _tick("questions")
                        continue
                    if part:
                        ok_counts[target] += 1
                    for q in part or []:
                        # _clean_questions 已归一化为 bank 多题型结构；
                        # 按 type+题干去重，但空题干（仅 composite 引导语允许）不参与去重、原样保留
                        stem = str(q.get("stem") or "").strip()
                        if stem:
                            key = f"{q.get('type')}|{stem}"
                            if key in seen_stem:
                                continue
                            seen_stem.add(key)
                        qs.append(q)
                    _tick("questions")
                draft["questions"] = qs
            else:  # paper
                paper: dict[str, Any] | None = None
                for i, b in enumerate(batches):
                    try:
                        part = _clean_paper(b, first=(from_batch + i == 0 and paper is None))
                    except (ValueError, SchemaError) as exc:
                        warnings.append(f"试卷识别某批失败已跳过（{exc}）")
                        _tick("paper")
                        continue
                    if not part:
                        _tick("paper")
                        continue
                    ok_counts[target] += 1
                    if paper is None:
                        paper = {"name": str(part.get("name", "")).strip()[:60] or "导入试卷",
                                 "questions": []}
                    paper["questions"].extend(part["questions"][:60])
                    _tick("paper")
                draft["paper"] = paper  # None = 未识别出试卷结构
    except (ValueError, SchemaError) as exc:
        # AI 未配置/格式不符：仅 concepts 目标可走启发式降级
        if "concepts" not in targets:
            raise
        LOG.warning("资料提取降级为标题启发式: %s", exc)
        draft = {"concepts": _heuristic_concepts("\n".join(batches))}
        source = "heuristic"
        warnings.append(f"AI 提取不可用（{exc}），已按标题层级降级提取概念；例题/试卷提取需配置 AI。")
    # concepts 目标全部批失败 → 回退启发式，保证有产出
    if source == "ai" and "concepts" in targets and ok_counts["concepts"] == 0:
        LOG.warning("概念提取全部批失败，降级为标题启发式")
        draft["concepts"] = _heuristic_concepts("\n".join(batches))
        source = "heuristic"
        warnings.append("概念 AI 提取全部失败，已按标题层级降级提取概念。")
    ai_calls = 0 if source == "heuristic" else done_calls
    return {"draft": draft, "source": source, "from_batch": from_batch,
            "to_batch": end, "batches_total": len(batches_all), "batches": len(batches),
            "batch_chars": size, "ai_calls": ai_calls, "truncated": False,
            "warnings": warnings}


def _heuristic_atomic_cards(text: str, max_cards: int = 30) -> list[dict[str, Any]]:
    """§27/§21.2 读书闭环基础版：结构化文本 → 原子卡（Zettelkasten 式单概念问答）。

    零依赖启发式（离线可用）：
    - Markdown 标题行 → 一个概念卡：提问「用自己的话解释 X」，答案为其后段落；
    - 无标题段落 → 取首句为概要卡。
    返回 [{question, answer, concept}]，最多 max_cards 张。
    """
    cards: list[dict[str, Any]] = []
    cur_title = ""
    cur_body: list[str] = []

    def _flush() -> None:
        nonlocal cur_title, cur_body
        body = "\n".join(cur_body).strip()
        if cur_title and body:
            cards.append({
                "question": f"用自己的话解释「{cur_title}」",
                "answer": body[:600],
                "concept": cur_title,
            })
        elif body and len(body) > 30:
            first = body.split("。")[0][:30]
            cards.append({
                "question": f"概括要点：{first}…",
                "answer": body[:600],
                "concept": "",
            })
        cur_title, cur_body = "", []

    for line in text.splitlines():
        m = re.match(r"^(#{1,3})\s+(.+)$", line.strip())
        if m:
            _flush()
            cur_title = re.sub(r"[#*`]", "", m.group(2)).strip()[:40]
        elif line.strip():
            cur_body.append(line.strip())
    _flush()
    return cards[:max_cards]


def extract_atomic_cards(text: str, subject: str = "",
                         use_ai: bool = True, max_cards: int = 30) -> list[dict[str, Any]]:
    """§27 读书闭环（基础版）：文本 → 原子卡草稿（不落库）。

    AI 可用（已配置密钥）时优先走 AI 生成更自然的 Q/A；任何异常或离线均降级为
    标题启发式，保证离线可用、零额外依赖。
    """
    text = (text or "").strip()
    if not text:
        return []
    if use_ai:
        try:
            from ai import call_ai, get_cached_settings
            if get_cached_settings().get("api_key"):
                from validate import validate_object
                data = validate_object(call_ai([
                    {"role": "system", "content": (
                        "你是学习卡片编辑。从给定文本提取原子知识卡，每张卡只考一个概念，"
                        '只返回 JSON：{"cards": [{"question": "问题", "answer": "答案", '
                        '"concept": "概念名"}]}。答案来自原文，不要编造。'
                    )},
                    {"role": "user", "content": text[:12000]},
                ], max_tokens=1500, tier="heavy", route="material", json_mode=True), {"cards": {"type": "array", "items": {"type": "object", "properties": {
                    "question": {"type": "string", "min_length": 3, "required": True},
                    "answer": {"type": "string", "min_length": 1, "required": True},
                    "concept": {"type": "string"},
                }}}})
                out = [{"question": str(c["question"])[:300], "answer": str(c["answer"])[:600],
                        "concept": str(c.get("concept", ""))[:40]} for c in (data.get("cards") or [])]
                if out:
                    return out[:max_cards]
        except Exception as exc:
            LOG.debug("原子卡 AI 生成降级为启发式: %s", exc)
    return _heuristic_atomic_cards(text, max_cards)


def apply_cards(cards: list[dict[str, Any]], subject: str) -> int:
    """§27：将确认的原子卡落库为错题（进入 FSRS 复习循环）。返回新增张数。"""
    if not cards:
        return 0
    subject = str(subject or "physics").strip() or "physics"
    count = 0
    with DB_LOCK, db() as conn:
        for c in cards:
            q = str(c.get("question", "")).strip()
            a = str(c.get("answer", "")).strip()
            if not q or not a:
                continue
            conn.execute(
                # 必须显式写 concept_ids：省略时落到列默认值，而 v10 的 DEFAULT 是
                # JSON 风格的 '[]'（config.py），与全代码约定的 CSV 格式不兼容。
                "INSERT INTO problems(title, course, topic, content, my_attempt, "
                "created_at, updated_at, subject, mastery, concept_ids) "
                "VALUES (?, '', ?, ?, '', ?, ?, ?, 1, '')",
                (q[:200], c.get("concept", "") or "", a[:2000], now(), now(), subject),
            )
            count += 1
    return count


def save_upload(name: str, stream, content_length: int = 0,
                max_bytes: int = 100 * 1024 * 1024) -> str:
    """上传文件落盘到 uploads/（分块写入，100MB 上限）。返回相对路径。

    content_length 为请求头声明的字节数（有 keep-alive 时不能读到 EOF）；
    文件名白名单化防路径穿越，仅接受 md/txt/pdf 扩展。
    """
    from config import APP_DIR
    if content_length < 0:
        raise ValueError("请求缺少合法的 Content-Length")
    if content_length > max_bytes:
        raise ValueError("文件超过 100MB 上限")
    safe = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fa5.]", "_", str(name).strip())[:80] or "upload.md"
    if not safe.lower().endswith((".md", ".txt", ".pdf")):
        safe += ".md"
    if ".." in safe or "/" in safe or "\\" in safe:
        raise ValueError("文件名不合法")
    uploads = APP_DIR / "uploads"
    uploads.mkdir(exist_ok=True)
    target = uploads / safe
    written = 0
    with open(target, "wb") as out:
        while written < content_length:
            chunk = stream.read(min(64 * 1024, content_length - written))
            if not chunk:
                break
            written += len(chunk)
            out.write(chunk)
    if written != content_length:
        target.unlink(missing_ok=True)
        raise ValueError("上传不完整，请重试")
    return str(target.relative_to(APP_DIR)).replace("\\", "/")


def apply_draft(payload: dict[str, Any], subject: str) -> dict[str, Any]:
    """用户确认后的写入：概念图谱 / 题库 / 试卷。返回各类计数。"""
    stats: dict[str, Any] = {"concepts_added": 0, "links_added": 0,
                             "questions_imported": 0, "paper": None}

    cp = payload.get("concepts")
    if cp and (cp.get("chapters") or cp.get("concepts")):
        import graph
        chapter_ids: dict[str, int] = {}
        for ch in cp.get("chapters", []):
            name = str(ch.get("name", "")).strip()[:40]
            if name:
                chapter_ids[name] = graph.add_concept(name, 0, subject=subject) or 0
        name_to_id: dict[str, int] = dict(chapter_ids)
        for c in cp.get("concepts", []):
            name = str(c.get("name", "")).strip()[:40]
            if not name:
                continue
            parent = chapter_ids.get(str(c.get("chapter", "")).strip(), 0)
            cid = graph.add_concept(name, parent, subject=subject)
            if cid:
                name_to_id[name] = cid
        # 概念关联（related → concept_links，双向幂等；G1 溯源：软性边 + 固定理由）
        with DB_LOCK, db() as conn:
            for c in cp.get("concepts", []):
                a = name_to_id.get(str(c.get("name", "")).strip())
                if not a:
                    continue
                for rel_name in (c.get("related") or []):
                    b = name_to_id.get(str(rel_name).strip())
                    if b and b != a:
                        cur = conn.execute(
                            "INSERT OR IGNORE INTO concept_links(concept_a, concept_b, relation, "
                            "strength, reason, evidence_ref) VALUES (?, ?, 'related', 'soft', ?, 'material-import')",
                            (min(a, b), max(a, b),
                             f"资料导入向导：AI 提取「{str(c.get('name', '')).strip()[:20]}」与「{str(rel_name).strip()[:20]}」的关联"))
                        stats["links_added"] += cur.rowcount
        stats["concepts_added"] = len([n for n in name_to_id.values() if n])

    qs = payload.get("questions")
    if qs:
        import bank
        # C3 来源标记：资料导入向导确认的题统一标 material（AI 从教材提取）
        for q in qs:
            if isinstance(q, dict):
                q["source"] = "material"
        result = bank.import_questions(qs, subject)
        stats["questions_imported"] = result["imported"]
        stats["questions_errors"] = result["errors"]

    pp = payload.get("paper")
    if pp and pp.get("questions"):
        import exam
        pid = exam.create_paper(str(pp.get("name") or "导入试卷"))
        added = exam.add_questions(pid, pp["questions"])
        stats["paper"] = {"id": pid, "added": added}
    return stats


def doc_text(doc_id: int) -> str:
    """从 RAG 已摄取文档取全文（按 chunk 顺序拼接）。"""
    chunks = rows(
        "SELECT content FROM rag_chunks WHERE doc_id = ? ORDER BY chunk_index, id", (doc_id,))
    if not chunks:
        raise ValueError("文档不存在或没有内容")
    return "\n\n".join(c["content"] for c in chunks)


def path_text(path: str) -> str:
    """从工作区路径读取文本（复用 RAG 提取器：md/txt/pdf）。"""
    import rag
    fp = rag._safe_relative(path)
    if not fp or not fp.is_file():
        raise ValueError("路径必须指向工作区内的文件")
    if fp.suffix.lower() == ".pdf":
        return "\n\n".join(rag._extract_pdf(fp))
    if fp.suffix.lower() in (".md", ".txt"):
        return rag._extract_text_file(fp)
    raise ValueError("仅支持 md / txt / pdf 文件")
