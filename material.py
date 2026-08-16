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
    return max(4000, min(24000, int(budget_tokens * 1.1)))

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

_QUESTION_SCHEMA = {
    "questions": {"type": "array", "items": {"type": "object", "properties": {
        "stem": {"type": "string", "min_length": 5, "required": True},
        "choices": {"type": "array", "items": {"type": "string", "min_length": 1}, "required": True},
        "answer": {"type": "integer", "min": 0, "required": True},
        "explain": {"type": "string"},
        "concept": {"type": "string"},
        "unit": {"type": "string"},
        "difficulty": {"type": "integer", "min": 1, "max": 5},
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


def _clean_concepts(batch_text: str) -> dict[str, Any] | None:
    from ai import call_ai
    prompt = [
        {"role": "system", "content": (
            "你是知识图谱编辑。从给定教材片段中提取章节与核心概念，只返回 JSON："
            '{"chapters": [{"name": "章节名"}], "concepts": [{"name": "概念名", '
            '"chapter": "所属章节名", "related": ["相关概念名"]}]}。'
            "要求：概念必须是学科概念（不要页码/题号/人名）；related 只填同片段出现过的概念；没有则给空数组。"
        )},
        {"role": "user", "content": batch_text},
    ]
    data = validate_object(call_ai(prompt, max_tokens=1200, tier="heavy", route="material"),
                           _CONCEPT_SCHEMA)
    if not data["chapters"] and not data["concepts"]:
        return None
    return data


def _clean_questions(batch_text: str) -> list[dict[str, Any]] | None:
    from ai import call_ai
    prompt = [
        {"role": "system", "content": (
            "你是题库编辑。从给定教材/试卷片段中提取选择题，只返回 JSON："
            '{"questions": [{"stem": "题干", "choices": ["A...", "B...", "C...", "D..."], '
            '"answer": 0, "explain": "解析", "concept": "考查概念", "unit": "所属单元", "difficulty": 3}]}。'
            "answer 是正确选项的下标（0 起）。只要客观选择题；片段没有选择题就返回空数组；"
            "不要自编题目或答案不确定的题。"
        )},
        {"role": "user", "content": batch_text},
    ]
    data = validate_object(call_ai(prompt, max_tokens=1800, tier="heavy", route="material"),
                           _QUESTION_SCHEMA)
    return data["questions"] or None


def _clean_paper(batch_text: str, first: bool) -> dict[str, Any] | None:
    from ai import call_ai
    name_rule = '"name": "试卷名称（从卷头识别）", ' if first else '"name": "", '
    prompt = [
        {"role": "system", "content": (
            "你是试卷录入员。判断给定片段是否为试卷，若是则提取题目清单，只返回 JSON："
            "{" + name_rule + '"questions": [{"qno": "题号", "topic": "考查知识点", '
            '"content": "题干摘要", "weight": 1}]}。'
            "weight 为分值/5（无分值则 1）。topic 必填。不是试卷或没有题目时 questions 给空数组。"
        )},
        {"role": "user", "content": batch_text},
    ]
    data = validate_object(call_ai(prompt, max_tokens=1200, tier="heavy", route="material"),
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
        if not name or name in seen_cp:
            continue
        seen_cp.add(name)
        acc["concepts"].append({
            "name": name,
            "chapter": str(cp.get("chapter", "")).strip()[:40],
            "related": [str(r).strip()[:40] for r in (cp.get("related") or []) if str(r).strip()],
        })


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
    try:
        for target in targets:
            if target == "concepts":
                acc: dict[str, Any] = {"chapters": [], "concepts": []}
                for b in batches:
                    part = _clean_concepts(b)
                    if part:
                        _merge_concepts(acc, part)
                    _tick("concepts")
                draft["concepts"] = acc
            elif target == "questions":
                qs: list[dict[str, Any]] = []
                seen_stem: set[str] = set()
                for b in batches:
                    part = _clean_questions(b)
                    for q in part or []:
                        stem = str(q["stem"]).strip()
                        if stem in seen_stem:
                            continue
                        seen_stem.add(stem)
                        qs.append({
                            "stem": stem[:500],
                            "choices": [str(c).strip()[:200] for c in q["choices"]],
                            "answer": int(q["answer"]),
                            "explain": str(q.get("explain", "")).strip()[:800],
                            "concept": str(q.get("concept", "")).strip()[:40],
                            "unit": str(q.get("unit", "")).strip()[:20],
                            "difficulty": int(q.get("difficulty", 2)),
                        })
                    _tick("questions")
                draft["questions"] = qs
            else:  # paper
                paper: dict[str, Any] | None = None
                for i, b in enumerate(batches):
                    part = _clean_paper(b, first=(from_batch + i == 0 and paper is None))
                    if not part:
                        _tick("paper")
                        continue
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
    ai_calls = 0 if source == "heuristic" else done_calls
    return {"draft": draft, "source": source, "from_batch": from_batch,
            "to_batch": end, "batches_total": len(batches_all), "batches": len(batches),
            "batch_chars": size, "ai_calls": ai_calls, "truncated": False,
            "warnings": warnings}


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
        # 概念关联（related → concept_links，双向幂等）
        with DB_LOCK, db() as conn:
            for c in cp.get("concepts", []):
                a = name_to_id.get(str(c.get("name", "")).strip())
                if not a:
                    continue
                for rel_name in (c.get("related") or []):
                    b = name_to_id.get(str(rel_name).strip())
                    if b and b != a:
                        cur = conn.execute(
                            "INSERT OR IGNORE INTO concept_links(concept_a, concept_b, relation) "
                            "VALUES (?, ?, 'related')", (min(a, b), max(a, b)))
                        stats["links_added"] += cur.rowcount
        stats["concepts_added"] = len([n for n in name_to_id.values() if n])

    qs = payload.get("questions")
    if qs:
        import bank
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
