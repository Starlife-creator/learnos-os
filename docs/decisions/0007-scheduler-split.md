# 0007 复习调度：FSRS 与 SM-2 分轨

状态：生效（2026-10）

## 决策
- 题目复习与闪卡复习统一调用 `scheduler.schedule_review()`，handler 和 cards 不再自己拼调度逻辑。
- FSRS 可用时，间隔和记忆态（state/stability/difficulty）完全由 FSRS 给出，不叠加逾期顺延。
- FSRS 不可用或抛异常时退回 SM-2。逾期顺延（逾期 ≥21 天降级为新学，≥5 天间隔减半）只在这一支生效。
- FSRS 的输入带上次复习日期：题目用本条 `reviews.created_at`，闪卡用 `cards.last_review`。

## 原因
- 以前的做法是先算 SM-2，再用 FSRS 覆盖间隔，然后叠一层顺延。FSRS 本来就按真实经过天数建模遗忘，再叠顺延等于把逾期罚了两次，而且实际排期和 FSRS 的稳定性对不上，后面训练参数时会被带偏。
- `fsrs_bridge.compute_fsrs_review` 以前从来没有设置 `card.last_review`。FSRS 因此把检索率 R 当成 0，每次复习都按"已经遗忘"来更新稳定性，"目标保持率"设置等于没生效。

## 后果
- 掌握度等级 `mastery`（1-5）仍由 SM-2 公式给出，只用于展示和图谱聚合，不参与 FSRS 调度。
- 升级后已有卡片的间隔会按 FSRS 重新计算，逾期复习的间隔通常比以前长。
- 测试：`tests/test_scheduler.py`（两条分支），`tests/test_handler.py` 里的 `test_review_complete_uses_scheduler` 和 `test_overdue_forgiveness_sm2_fallback`。
