"""导出领域层：题目快照、Anki CSV、复习日程 ICS 的数据装配与格式渲染。

三种导出都是「查一次库 → 拼一段文本/字典」，与 HTTP 无关；handler 只负责把
结果写进响应（含 Content-Disposition）。这样 CSV/ICS 的转义与行尾都有地方可以
直接单元测试，不必起服务。
"""
from __future__ import annotations

import csv
import io
import json

from db import now, rows


def snapshot(subject: str) -> dict:
    """JSON 导出：本题库题目 + 全量提示 + 全量复习记录（保持既有口径）。"""
    problems = rows(
        "SELECT id, title, course, topic, content, my_attempt, error_type, error_path, "
        "trap_note, shortcut, fix_action, tags, tags_status, mastery, created_at, updated_at, subject "
        "FROM problems WHERE subject = ? ORDER BY id", (subject,))
    for p in problems:
        try:
            p["tags"] = json.loads(p["tags"]) if p["tags"] else []
        except (json.JSONDecodeError, TypeError):
            p["tags"] = []
    return {
        "version": 1,
        "exported_at": now(),
        "problems": problems,
        "hints": rows("SELECT problem_id, level, content, created_at FROM hints ORDER BY id"),
        "reviews": rows("SELECT problem_id, due_date, interval_days, result, completed, created_at "
                        "FROM reviews ORDER BY id"),
    }


def anki_csv(subject: str) -> str:
    """Anki 导入 CSV：question|answer|tags（含 UTF-8 BOM，兼容 Anki 桌面端）。"""
    items = rows("""
        SELECT p.id, p.title, p.course, p.topic, p.content, p.my_attempt, p.tags,
               p.error_path, p.trap_note, p.shortcut, p.fix_action,
               (SELECT GROUP_CONCAT(content, '\n') FROM hints h WHERE h.problem_id = p.id AND h.level = 3) AS answer_hint
        FROM problems p WHERE p.subject = ? ORDER BY p.id
    """, (subject,))
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\n")
    for p in items:
        front = f"{p['title']}\n{p['content']}".strip()
        back_parts = []
        if p.get("my_attempt"):
            back_parts.append(f"我的尝试：{p['my_attempt']}")
        if p.get("answer_hint"):
            back_parts.append(f"解题框架：{p['answer_hint']}")
        if p.get("shortcut"):
            back_parts.append(f"捷径：{p['shortcut']}")
        if p.get("fix_action"):
            back_parts.append(f"改进：{p['fix_action']}")
        back = "\n".join(back_parts) or "（无解析）"
        tags = " ".join(t for t in [p["course"], p["topic"]] if t)
        try:
            tag_list = json.loads(p.get("tags") or "[]")
            if isinstance(tag_list, list):
                tags = " ".join(str(t).replace(":", "_") for t in tag_list if str(t).strip())
        except json.JSONDecodeError:
            pass
        writer.writerow([front, back, tags])
    return "\ufeff" + buf.getvalue()


def ics(subject: str) -> str:
    """复习日程 .ics：未完成的 due 复习任务导出为 VEVENT（CRLF 行尾）。"""
    due = rows(
        "SELECT r.id, r.due_date, r.interval_days, p.title FROM reviews r "
        "JOIN problems p ON p.id = r.problem_id WHERE r.completed = 0 AND p.subject = ? "
        "ORDER BY r.due_date", (subject,))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//LearnOS//CN",
        "CALSCALE:GREGORIAN",
        # 日历名沿用历史硬编码（纯搬迁，不改可见输出）
        "X-WR-CALNAME:物理复习日程",
    ]
    for r in due:
        stamp = r["due_date"].replace("-", "") + "T090000"
        lines += [
            "BEGIN:VEVENT",
            f"UID:pso-review-{r['id']}@learnos-os",
            f"DTSTART:{stamp}",
            f"DTEND:{stamp}",
            f"SUMMARY:复习：{r['title']}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)
