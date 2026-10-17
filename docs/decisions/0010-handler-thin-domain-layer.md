# 0010 handler 只做 HTTP 适配，业务逻辑下沉领域层

状态：生效

## 决策

`handler_*.py` 是 HTTP 适配层，只允许做三件事：解析请求、调用领域模块、写响应。
SQL、聚合、业务规则一律放在领域模块（`reports.py`、`graph.py`、`bank.py`、`cards.py`、
`review.py`、`mastery.py`、`scheduler.py`）。

领域层不得反向 import HTTP 层（`handler*`、`app`、`http.server`）。

落地方式：**棘轮（ratchet）**，不搞一次性大重构。

- `tests/test_fitness_functions.py::TestHandlerSqlBudget` 把每个 `handler_*.py` 的
  「SQL 语句条数」冻结为上限，只减不增；新增 `handler_*.py` 必须同时登记预算，
  否则守卫可被绕过。计数只认**语句骨架**（`SELECT` / `INSERT … INTO` /
  `UPDATE <表> SET` / `DELETE FROM`），不认单独出现的关键字 —— 否则 docstring 里的
  「写/删/还原操作鉴权闸门（do_POST/PUT/DELETE）」和路由正则
  `/api/cards/(\d+)/delete` 都会被算成 SQL，「剩余预算」这个进度条就失真了
  （第四批顺手收紧了度量，因此表里的数字与前几批不可直接对比）。
- `TestDomainLayerPurity` 断言领域层不 import HTTP 层，且 `reports.py` 内不出现
  `json_response` / `self.`。

首批：`handler_reports.py` 的全部 37 条 SQL（15 个统计函数 + 仪表盘/进步数据装配）
下沉为 `reports.py`（621 行），`handler_reports.py` 由 637 行缩到 38 行，只剩 7 个
「取 subject → 写响应」的适配方法。

第二批：`handler_problems.py` 的题目读写（增删改查、导入、提示、变式、导出、
`_ensure_problem`）下沉为 `problems.py`，裸 SQL 由 43 条降到 15 条。剩下的 15 条
全部属于 `/api/problems/batch`（批量操作的状态机，一次事务里多点写，形状特殊，
留待单独一批）。

第三批：`handler_reviews.py` 的复习会话下沉为 `review_session.py`：队列装配
（逾期优先 + 错因权重 + FSRS 稳定性 + 费曼漏点徽章 + 每日上限 + 知识点交错）、
完成复习（调度 + 落库 + 掌握度事件 + 掌握度日快照）、顺延、今日小结。裸 SQL 由 11
条降到 0，文件从 154 行降到 33 行的纯适配层；`handler_base._interleave` 一并搬进
`review_session.interleave`（它是复习队列的规则，不再被第二个入口共享）。

领域层开始有**自己的错误类型**：`domain_errors.py` 的 `DomainError`（带
`message` / `status`）与 `problems.ProblemsError`、`review_session.ReviewError`。
handler 层不再逐处拼 400/404 响应，而是捕获 `DomainError` 后统一翻译 —— 这样
「同一业务错误在不同 handler 里写不同文案」的分叉被结构性地消掉了。

第四批（收尾）：`handler.py` 的学科容器与搜索下沉为 `subjects.py`（总览、新增、
按 scope 级联删除、全局搜索），运维指标采集下沉为 `metrics.py`，另外
`problems.delete_problem` / `problems.stability_sample` / `exam.paper_exists` /
`db.save_settings` 各收掉一条。`handler.py` 由 1157 行降到 998 行，SQL 由 21 条
（新度量下 6 条）降到 **2 条**；全仓 handler 层合计 **17 条**。
审计（`auth.audit` 需要客户端 IP）与 `LOG.info` 是 HTTP 层关注点，留在 handler。

## 原因

迁移前 handler 层的 SQL 密度：`handler_reports.py` 51 处、`handler_problems.py`
45 处、`handler.py` 24 处，而较新的 `handler_cards.py` / `handler_learn.py` /
`handler_social.py` 基本为 0。

这带来三个具体后果：

1. **不可脱离 HTTP 测试**。统计口径（平均掌握度、保持率、压力分、遗忘曲线分桶）
   只能通过起 HTTP 服务 + 构造数据库来验证，无法单元测试，于是这些口径长期没有
   直接断言。
2. **规则会分叉**。同一件事在多个入口各写一遍 —— 例：`_handle_trend` 与
   `_handle_analytics` 都曾漏传 `subject`，导致切学科后趋势图恒显示 physics
   （两处都留了注释，是同一 bug 的两次独立发生）。
3. **复用不了**。`reports.*` 现在是纯函数，CLI、导出、日后的北极星指标都能直接调。

## 后果

- 收益是**可测 + 可复用 + 防回流**，而不是「文件变短」。`reports.py` 的每个函数
  都能用 `row`/`rows` 读的真实库直接断言，`#13 北极星指标`（实际保持率 vs FSRS
  目标保持率）将直接加在 `reports.py` 上。
- 成本：`reports.py` 与 handler 之间多一层调用；跨层改动要同时看两个文件。
- 行为保持：本批次是纯搬迁，SQL 文本（规范化后逐条比对）、参数、取整、排序、
  默认值、返回键、`except Exception → LOG.debug + return {}` 降级路径全部一致。
- **唯一有意变更**：`GET /api/gamification` 原先直接调用 `gamification.state()`，
  异常会 500；现在走 `reports.game_state()`，异常降级为空字典（与仪表盘卡片同款
  韧性策略，§1.3）。
- 剩余预算即待办清单：`handler.py` 2（有意保留，见测试里 `HANDLER_SQL_BUDGET` 注释：
  `_get_or_404` 的通用查存在助手 + `/api/fsrs/train` 的一次全表采样）、
  `handler_problems.py` 14（`/api/problems/batch` 批量状态机）、`handler_material.py` 1、
  `handler_oral.py` 1。桌面上的数字就是进度条 —— 每下沉一批就把对应数字调小，
  守卫不会因此失败（只拦上升）。
- 副作用收益（第三、四批换来的）：复习队列的排序规则、每日上限、交错策略、完成复习
  的事务边界（`tests/test_review_session.py`，17 个用例）与学科级联删除的 scope 边界
  （`tests/test_subjects.py`，16 个用例）现在都有直接单元测试，不必起 HTTP 服务、
  不必构造会话。此前「逾期优先」「删图谱不该动个人数据」这类承诺只有人工试过。
