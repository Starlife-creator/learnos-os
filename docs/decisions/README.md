# 架构决策记录（ADR）

已定稿、需要跨机器/跨会话保留的设计决策。草稿与探索仍放本地 `OPTIMIZATION_PLAN.md`（不入库）。

格式：每篇半页以内，写清「决策 / 原因 / 后果」。被推翻时不删，改状态为「已废弃」并链接新决策。

| 编号 | 标题 | 状态 |
|---|---|---|
| [0001](0001-zero-dependency-local-first.md) | 零第三方依赖、本地优先、工作区隔离 | 生效 |
| [0002](0002-ai-writes-draft-confirm.md) | AI 写入走草稿确认或结构化校验 | 生效 |
| [0003](0003-secrets-never-in-db.md) | 密钥不进数据库，日志脱敏 | 生效 |
| [0004](0004-mastery-event-sourcing.md) | 掌握度变更事件溯源，单一写入口 | 生效（收敛进行中） |
| [0005](0005-explicit-non-goals.md) | 明确不做清单 | 生效 |
| [0006](0006-test-infrastructure.md) | 测试入口、临时目录与日志隔离 | 生效 |
| [0007](0007-scheduler-split.md) | 复习调度：FSRS 与 SM-2 分轨 | 生效 |
