"""互操作导出（§24.3）：将错题本导出为 CSV / Markdown，便于迁移与分享。

- CSV：UTF-8 with BOM（Excel/Numbers 友好），列稳定。
- Markdown：人类可读复习清单，默认隐藏答案（include_answers=False 用于分享）。
导出不含 FSRS 调度等内部字段；答案可选，默认在分享场景隐藏。
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any

from db import row, rows


def _fetch_problems(subject: str) -> list[dict[str, Any]]:
    data = rows(
        "SELECT id, title, course, topic, content, my_attempt, error_type, error_path, "
        "trap_note, shortcut, fix_action, tags, mastery, created_at, subject "
        "FROM problems WHERE subject = ? ORDER BY id",
        (subject,),
    )
    out = []
    for p in data:
        d = dict(p)
        try:
            d["tags"] = json.loads(d["tags"]) if d["tags"] else []
        except (json.JSONDecodeError, TypeError):
            d["tags"] = []
        out.append(d)
    return out


def export_csv(subject: str, include_answers: bool = True) -> str:
    """返回带 BOM 的 CSV 文本。"""
    problems = _fetch_problems(subject)
    buf = io.StringIO()
    buf.write("\ufeff")
    cols = ["id", "title", "course", "topic", "error_type", "mastery",
            "content", "my_attempt", "error_path", "trap_note", "shortcut",
            "fix_action", "tags", "created_at", "subject"]
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    for p in problems:
        row_out = dict(p)
        if not include_answers:
            row_out["my_attempt"] = ""
            row_out["error_path"] = ""
            row_out["fix_action"] = ""
        if isinstance(row_out["tags"], list):
            row_out["tags"] = ";".join(row_out["tags"])
        writer.writerow(row_out)
    return buf.getvalue()


def export_md(subject: str, include_answers: bool = True) -> str:
    """返回 Markdown 复习清单。"""
    problems = _fetch_problems(subject)
    lines = [f"# LearnOS 错题本导出（{subject}）", "",
             f"> 共 {len(problems)} 题。{'含答案。' if include_answers else '已隐藏答案（分享用）。'}", ""]
    for i, p in enumerate(problems, 1):
        tags = " ".join(f"`{t}`" for t in (p["tags"] or []))
        lines.append(f"## {i}. {p['title'] or '(无标题)'}")
        lines.append(f"- 课程: {p['course'] or '-'} ｜ 主题: {p['topic'] or '-'} ｜ 错因: {p['error_type']} ｜ 掌握: {p['mastery']}/5")
        if tags:
            lines.append(f"- 标签: {tags}")
        lines.append(f"- 题目: {p['content']}")
        if include_answers:
            if p["my_attempt"]:
                lines.append(f"- 我的作答: {p['my_attempt']}")
            if p["error_path"]:
                lines.append(f"- 错因路径: {p['error_path']}")
            if p["trap_note"]:
                lines.append(f"- 易错点: {p['trap_note']}")
            if p["shortcut"]:
                lines.append(f"- 速记: {p['shortcut']}")
            if p["fix_action"]:
                lines.append(f"- 修正动作: {p['fix_action']}")
        lines.append("")
    return "\n".join(lines)
