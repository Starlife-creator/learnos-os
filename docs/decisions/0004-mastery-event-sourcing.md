# 0004 掌握度变更事件溯源，单一写入口

状态：生效

## 决策
- 掌握度每一次变化都写一条 `mastery_events`（入口、证据、旧值、新值、revision），与状态更新在同一事务内完成；值不变不记。
- 题目层：`problems.mastery` 只经 `mastery.set_problem_mastery()` 修改（事件 `item_type='problem'`，`item_id`=题目 id，`concept_id`=0）。
- 概念层：`concept_progress` 只经 `graph.update_progress()` 全量重算写入（事件 `item_type='concept'`）。
- 量纲：题目沿用 1-5 级整数（SM-2 与界面依赖）；聚合与事件一律用 0-1，换算只用 `mastery.level_to_unit()`。
- `mastery_log` 保留，不并入事件表：它是每日平均掌握度快照（趋势图数据源），语义与变更事件不同。

## 原因
趋势图、撤销、错因诊断、熟悉度展示与后续的北极星指标（实际保持率）都依赖完整的变更历史。多入口直写会让事件日志残缺，用户手动修改（`entry_point=edit`）与算法结果（`review`/`bank`）也无法区分。

## 现状
v36 起 `mastery_events` 增加 `item_type`/`item_id`。复习、题库再次答错、错题编辑三处直写已收敛。

## 后果
`tests/test_fitness_functions.py::TestMasteryWriteGuard` 扫描根目录模块，出现 `UPDATE problems SET ... mastery =` 即失败。新建题目（INSERT）不受限。
