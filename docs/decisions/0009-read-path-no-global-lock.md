# 0009 读路径不持全局锁，连接失效自愈

状态：生效

## 决策

`db.rows()` 不再获取 `DB_LOCK`。改为「捕获连接已失效 → 重建线程连接 → 重试一次」：

- 捕获 `sqlite3.ProgrammingError` / `sqlite3.OperationalError`；
- 经 `_is_conn_gone(exc)` 判定是否为「连接已被他线程关闭」类错误
  （消息含 `closed database` / `cannot operate on a closed` / `no such table` /
  `unable to open database` / `disk i/o error`）；
- 仅当 `_TLS.depth == 0`（不在嵌套事务中）才 `_discard_thread_conn()` 并重试；
- 两次都失败则抛最后一次异常。

`backup.auto_backup_if_due()` 的 `close_all_connections()` + `shutil.copy2` 补上 `DB_LOCK`
（原先裸奔，才是真正的竞态源；`restore_backup` 早已持锁）。

## 原因

原实现让**所有**读都串行在同一把全局锁上，只为防一个极窄的竞态：备份线程
`close_all_connections()` 关掉读线程正在用的连接。代价是单机学习工具的全部读请求排队。

真正需要保护的是「关连接 vs 用连接」这一小段，而不是每一次 `SELECT`。把保护范围收窄到
备份路径 + 读侧自愈，既消除竞态又不损失并发。

## 后果

- 读并发不再被全局锁串行化；`row()` 走 `rows()` 自动受益。
- 嵌套事务内**不**重试（连接已被关闭时继续用旧连接会破坏事务语义），直接抛错由上层处理。
- 错误判定靠消息匹配（stdlib sqlite3 不提供错误码细分），新增措辞需同步
  `_is_conn_gone`；`tests/test_db.py::TestReadPathNoGlobalLock` 覆盖两条路径。
