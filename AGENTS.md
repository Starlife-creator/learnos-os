# 项目规则（强制约束）

## 工作区隔离红线（最高优先级）
- 所有修改、生成文件、运行产物必须严格限制在本工作区
  `E:\tool\biancheng\AI project 2\learnos-os` 内。
- 禁止对工作区外部造成任何影响：
  - 不写工作区外的任何文件（包括系统临时目录、用户目录）
  - 不修改系统设置、注册表、环境变量（系统级）、服务、计划任务、启动项
  - 不安装全局依赖、不修改其他项目
  - 不访问、读取、修改工作区以外的目录（除非用户明确要求）
- 测试、构建、运行产生的临时数据也应在工作区内（如 `tests/.tmp/`），
  用完清理或忽略提交。

## 其他
- 保持零第三方依赖主路径；可选依赖缺失时必须有降级路径。
- 所有 AI 写入走「草稿确认」或「结构化校验」，不静默落库。
- 密钥绝不写入数据库，日志脱敏。

## 协作 / 群聊约定
- 群里 @ 必须用**英文直引号**标签 <at user_id="…"></at>，id 用**发送方应用（你自己）视角下**的对方 id——各 bot 用自己 secret 从 `/bot/v3/info` 取到的自报 id 是它自己视角的，在你的消息里一个都 @ 不中；中文弯引号会让标签退化成普通文字。
- 判定是否送达：看聊天转写标题行有没有 `·N mention(s)`；没有就是没送到。
- 手头没有验证过能用的 id 时，回执直接发群里即可（管家会读全群记录），不必硬 @。

## 项目暂停状态与恢复方法（2026-11 起暂停，未解冻）
- 停止点：`eb23fb8`（2026-10-17 阶段3 收尾）；此后主树为 `d1b51f1`（.gitignore +4 行）及之后的 AGENTS.md 文档提交（`d3b4942`、`ad8d8e7`，以及本行所在的这次提交）——**零源码改动**。
- 核对事实（2026-11-14 实测）：`git diff --stat eb23fb8..HEAD` = `.gitignore +4` / `AGENTS.md +16`（纯文档提交会让 AGENTS.md 行数继续累加）；落地条件 e)「主树必须干净」仍满足，`git apply` 仍须带 `--exclude=.gitignore`。
- 评审材料：`.tmp/pr2-APPLY-NOTES.md`（清单/证据/落地五步）。补丁本体
  `.tmp/pr2-updated-worktree.diff`（首选）与 `.tmp/pr2-worktree.diff`，**只能二选一**。
- 落地命令须带 `--exclude=.gitignore`：
  `git apply --exclude=.gitignore .tmp/pr2-updated-worktree.diff`
- 基线基准数字：主树 **689 passed + 16 subtests**；打上 pr2-updated 后 **694 passed + 16
  subtests**（差的 5 个即新增测试）。数字对不上就说明补丁或基线已变，重新论证再落地。
- 测试入口：`python run_tests.py`（唯一权威，别直接 unittest discover）；lint 为
  `ruff check`（dev-only、非阻塞）。
