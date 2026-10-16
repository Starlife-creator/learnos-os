# 0010 handler 只做 HTTP 适配，业务逻辑下沉领域层

状态：生效

## 决策

`handler_*.py` 是 HTTP 适配层，只允许做三件事：解析请求、调用领域模块、写响应。
SQL、聚合、业务规则一律放在领域模块（`reports.py`、`graph.py`、`bank.py`、`cards.py`、
`review.py`、`mastery.py`、`scheduler.py`）。

领域层不得反向 import HTTP 层（`handler*`、`app`、`http.server`）。

落地方式：**棘轮（ratchet）**，不搞一次性大重构。

- `tests/test_fitness_functions.py::TestHandlerSqlBudget` 把每个 `handler_*.py` 的
  「含 SQL 关键字的字符串字面量条数」冻结为上限，只减不增；新增 `handler_*.py`
  必须同时登记预算，否则守卫可被绕过。
- `TestDomainLayerPurity` 断言领域层不 import HTTP 层，且 `reports.py` 内不出现
  `json_response` / `self.`。

首个落地批次：`handler_reports.py` 的全部 37 条 SQL（15 个统计函数 + 仪表盘/进步
数据装配）下沉为 `reports.py`（621 行），`handler_reports.py` 由 637 行缩到 38 行，
只剩 7 个「取 subject → 写响应」的适配方法。

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
- 剩余预算即待办清单：`handler_problems.py` 43、`handler.py` 21、
  `handler_reviews.py` 11。桌面上的数字就是进度条 —— 每下沉一批就把对应数字调小，
  守卫不会因此失败（只拦上升）。
