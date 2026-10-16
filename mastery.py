"""掌握度单一写入口（ADR 0004）。

- 题目掌握度 `problems.mastery` 是 1-5 级整数（SM-2 与界面沿用）；跨对象聚合、
  事件日志一律使用 0-1 连续值，换算只在这里定义（`level_to_unit`）。
- 任何对 `problems.mastery` 的修改都必须经 `set_problem_mastery()`：同一事务内
  更新状态并写一条 `mastery_events`（item_type='problem'），值不变则不记。
- 概念掌握度 `concept_progress` 由 `graph.update_progress()` 全量重算并写事件
  （item_type='concept'），是概念层唯一写路径。
守卫测试：tests/test_fitness_functions.py::TestMasteryWriteGuard。
"""
from __future__ import annotations

from typing import Any

from db import now

LEVEL_MIN = 1
LEVEL_MAX = 5


def clamp_level(value: Any) -> int:
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError):
        v = LEVEL_MIN
    return max(LEVEL_MIN, min(LEVEL_MAX, v))


def level_to_unit(level: Any) -> float:
    """1-5 级 → 0-1 连续值（与 graph 聚合同口径：level / 5）。"""
    try:
        v = float(level or 0)
    except (TypeError, ValueError):
        v = 0.0
    return max(0.0, min(1.0, v / LEVEL_MAX))


def set_problem_mastery(conn: Any, problem_id: int, level: Any, entry_point: str,
                        evidence: str = "") -> bool:
    """写题目掌握度并落事件（调用方提供事务连接）。返回值是否实际变化。"""
    new_level = clamp_level(level)
    cur = conn.execute("SELECT mastery, subject FROM problems WHERE id = ?",
                       (int(problem_id),)).fetchone()
    if cur is None:
        return False
    old_level = int(cur["mastery"] or 0)
    if old_level == new_level:
        return False
    stamp = now()
    conn.execute("UPDATE problems SET mastery = ?, updated_at = ? WHERE id = ?",
                 (new_level, stamp, int(problem_id)))
    rev = conn.execute(
        "SELECT COUNT(*) AS n FROM mastery_events WHERE item_type = 'problem' AND item_id = ?",
        (int(problem_id),)).fetchone()
    conn.execute(
        "INSERT INTO mastery_events(subject, concept_id, item_type, item_id, entry_point, evidence, "
        "revision, prev_mastery, cur_mastery, created_at) VALUES (?, 0, 'problem', ?, ?, ?, ?, ?, ?, ?)",
        (cur["subject"] or "", int(problem_id), entry_point, (evidence or "")[:200],
         int(rev["n"] if rev else 0) + 1, level_to_unit(old_level), level_to_unit(new_level), stamp))
    return True
