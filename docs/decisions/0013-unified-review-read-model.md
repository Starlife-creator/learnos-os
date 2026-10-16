# 0013 统一可复习项读模型：视图合流，写路径不合表

状态：生效

## 决策

引入两个**只读视图**（`db.py` 迁移 v37），把「题目」与「闪卡」在**读**的一侧合流：

- `review_items`：一行一个可复习项，列 `item_type`(`'problem'`/`'card'`)、`item_id`、
  `subject`、`label`、`due_date`、`interval_days`、`state`、`stability`、`difficulty`、
  `repetition`、`last_review`。题目**全部**保留（没有待复习行时 `due_date` 为
  `NULL`，天然被 `due_date <= ?` 排除）；卡片只收 `status='active'` 且 `due_date != ''`
  （与闪卡页角标同口径）。
- `review_log`：已完成复习的历史事实，列 `item_type`、`item_id`、`subject`、`rating`
  （`reviews.result` 是文本，统一 `CAST` 成整数）、`created_at`、`due_date`、
  `interval_days`、`log_id`。卡片排除 `undone=1` 的作废行。

**写路径不变**：题目仍写 `reviews`/`problems`，卡片仍写 `card_reviews`/`cards`，
不迁数据、不合表。所有学习活动类聚合（仪表盘、压力指数、今日任务、遗忘预测、
遗忘曲线、成熟占比、周报/月报、北极星、学习者节奏、行动规则）改为读视图。

**刻意保留的一处口径不一致**：`dashboard.due`、`progress.due_today`、
`agent_rules` 的 `due`、复习页队列都仍是**题目**口径；卡片由 `due_cards` /
`due_by_type` 单独暴露，行动规则也单独出一条「复习 N 张到期闪卡」。

## 原因

1. **需求缺口是「看不见」，不是「没建模」**。在此之前**用户复习了 20 张闪卡，
   今日任务、压力指数、周报、北极星里一张都不体现** —— 闪卡的复习行为对整条
   反馈回路不存在。缺的是读口径，不是表结构。
2. **合表要改所有写点并迁存量数据，风险远大于收益**。两套表的列语义并不对齐：
   `reviews.result` 是文本评分、`card_reviews.rating` 是整数且带 `undone` 撤销标记；
   卡片还有 `prev_*` 快照列。合表要同时改 `cards.py`、`handler_reviews.py`、
   `scheduler.py`、备份/回收站/重置逻辑，并写一次不可逆的数据迁移。
3. **视图用相关子查询而不是 `JOIN`**：一题恰有一行未完成 `reviews` 行，但一旦出现
   多行，`JOIN` 会把题目放大成多行、让所有计数失真。（这条是实测教训：
   第一版用 `INNER JOIN`，`mature_share` 直接把「从未排过复习行的题目」整批藏掉，
   分母从 5 变成 4，结构性指标静默偏大。）
4. **两个入口不能合成一个数**。卡片在闪卡页复习、错题在复习页复习。如果把
   `due` 合并成「题目 + 卡片」，用户点进复习页只会看到一部分，数字与页面不符
   是比多一个字段更糟的体验。

## 后果

- `tests/test_review_model.py` 用一份最小数据集（3 题 + 2 张 active 卡 + 1 张停用卡 +
  1 条作废卡片复习）逐个消费者钉住口径：任何一处退回 problems-only 都会失败。
  其中 `mature_share == 0.2`（分母 5，不是 4）正是上面第 3 条的回归守卫。
- 视图列是**契约**：新增列必须同步 `docs` 与消费方；`review_items` 不承担写语义，
  任何 `INSERT/UPDATE` 视图的尝试都必须被拒绝（SQLite 默认即拒绝）。
- 仍然保留 problems-only 的两处消费方（`handler_reviews` 今日小结、
  `handler.py` 的 FSRS 训练端点）是**题目复习页的专属视图**，属于入口语义，
  不是漏改；改动前需先决定该页面是否要展示卡片。
- `reports.north_star()` 的样本口径由 ADR 0011 的「`problems.repetition > 0`」改为
  「在 `review_log` 中有前次记录」，题目与卡片同权（复习行为不分类型）。
