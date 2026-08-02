"""AI 口试模块：围绕一个主题进行五轮追问。"""
from __future__ import annotations

import json
from typing import Any

from config import LOG
from db import db, now, row, DB_LOCK
from ai import call_ai

ORAL_QUESTIONS = [
    '请不用公式，先用物理图像解释「{topic}」的核心含义。',
    "这个结论成立需要哪些前提？请至少说出两个。",
    "请给出一个容易误用该概念的反例，并解释错在哪里。",
    "如果某个关键参数趋近于零或无穷大，结果应该怎样变化？",
    "请设计一种实验或数值方法来检验你刚才的解释。",
]


def start_oral(topic: str) -> tuple[int, str]:
    question = ORAL_QUESTIONS[0].format(topic=topic)
    try:
        question = call_ai([
            {"role": "system", "content": "你是大学物理口试老师。一次只问一个简洁问题，不给答案。"},
            {"role": "user", "content": f'围绕「{topic}」提出第一个概念理解问题。'},
        ], max_tokens=180)
    except Exception as exc:
        LOG.warning("口试 AI 调用失败，使用内置问题: %s", exc)

    transcript = [{"role": "assistant", "content": question}]
    with DB_LOCK, db() as conn:
        cursor = conn.execute(
            "INSERT INTO oral_sessions(topic, transcript, created_at) VALUES (?, ?, ?)",
            (topic, json.dumps(transcript, ensure_ascii=False), now()),
        )
        return int(cursor.lastrowid), question


def continue_oral(session: dict[str, Any], answer: str) -> str:
    transcript = json.loads(session["transcript"])
    transcript.append({"role": "user", "content": answer})
    turn = sum(1 for item in transcript if item["role"] == "user")

    if turn >= 5:
        instruction = "这是最后一轮。简短评价回答，指出一个掌握点和一个薄弱点，然后给出复习建议。以【口试结束】开头。"
    else:
        instruction = "先用一句话指出回答中最需要修正或深化之处，然后只提出一个追问。不要给完整答案。"

    try:
        messages = [{"role": "system", "content": f"你是严格的大学物理口试老师。主题是{session['topic']}。{instruction}"}]
        messages.extend(transcript[-8:])
        reply = call_ai(messages, max_tokens=350)
    except Exception as exc:
        LOG.warning("口试 AI 调用失败，使用内置回复: %s", exc)
        if turn >= 5:
            reply = "【口试结束】你已经完成五轮回答。请回看哪些回答没有说明适用条件，并选择其中一个概念在明天重新口述。"
        else:
            idx = min(turn, len(ORAL_QUESTIONS) - 1)
            reply = ORAL_QUESTIONS[idx].format(topic=session["topic"])

    transcript.append({"role": "assistant", "content": reply})
    status = "finished" if "【口试结束】" in reply or turn >= 5 else "active"
    with DB_LOCK, db() as conn:
        conn.execute(
            "UPDATE oral_sessions SET transcript = ?, status = ? WHERE id = ?",
            (json.dumps(transcript, ensure_ascii=False), status, session["id"]),
        )
    return reply
