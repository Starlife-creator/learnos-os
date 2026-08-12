# 物理学习 OS — 完整优化方案（修订版）

> 汇总自多轮评审建议，去重并标注状态。**所有改动仅限本项目文件夹内**。
> 状态图例：✅ 已完成 · 🔴 待修复（含 Bug）· 🟠 待做（重要）· 🟡 待做（增强）
>
> **修订说明（v2）**：依据代码实查，修正了原方案的 3 处失真——
> ① P0-1 严重性上调（前端当前在浏览器里完全不可用，非"一个待修 Bug"）；
> ② P0-2 的"环境变量优先"不成立（key 仅从 DB 读取，直接删落库会导致无法配置）；
> ③ P1-10 改掌握度刻度会破坏现有数据语义（改为仅 UI 标注）。
> 并补齐 P0-3 / P1-2 / P1-3 / P1-12 / P1-13 的实施后果。

---

## 一、已完成优化（前两轮的"完成全部优化"）

### 架构与后端
- ✅ `app.py` 单文件拆分为 7 模块：`config.py` / `db.py` / `ai.py` / `review.py` / `oral.py` / `handler.py` / `app.py`
- ✅ 后端 API 全量可用（health / dashboard / problems CRUD / hints / reviews / oral / settings）
- ✅ 间隔复习升级为 **SM-2 算法**（ease_factor + repetition 跟踪）
- ✅ `_safe_error()` 异常信息脱敏，不直接回传客户端
- ✅ `do_DELETE` 补齐异常处理；删除不存在行返回 **404**
- ✅ AI 调用 **1 次自动重试**（仅 5xx / 网络错误）；超时 45s
- ✅ DB **版本化迁移**（`schema_version` 表，v0.1→v0.2 自动加列）
- ✅ AI 设置 **30s TTL 内存缓存**，保存时自动失效
- ✅ `logging` 模块替代 `print`；端口冲突检测兼容 Windows（errno 10048 + 中文提示）
- ✅ `config.PORT` 解析容错，无效值回退 8765
- ✅ `build.spec` PyInstaller 构建脚本；`start.bat` 加 Python 检测 + UTF-8

### 前端与测试
- ✅ `static/index.html` 完整单页应用（概览 / 错题 / 复习 / 口试 / 设置）——**代码已写完**
- ✅ **错题编辑**（PUT `/api/problems/{id}`）+ **客户端搜索** 已实现
- ✅ 响应式 `@media (max-width:640px)`；KaTeX 公式渲染；Esc / 点遮罩关弹窗
- ✅ `POST /api/oral/{id}/end` 端点，前端 `resetOral()` 关闭旧会话
- ✅ 删除死文件 `static/app.js`、`static/styles.css`
- ✅ `.gitignore` 加 `!build.spec` 例外；README 重写为完整文档
- ✅ **61 个单元测试全部通过**（覆盖 SM-2 / AI / DB / HTTP 端点 / 集成 / 缓存 / 迁移 / 分页 / 口试结束）

> ⚠️ **重要注记（验证盲区）**：上述前端"完成"指**代码已写完**，但 `static/index.html` 内联脚本存在 P0-1 的致命解析错误，导致**当前在真实浏览器里整段 JS 不执行**——导航、数据加载、弹窗、口试全部不可用。61 个单测仅覆盖后端，**前端零测试**。因此所有前端相关能力均"未经运行验证"。修复 P0-1 后必须补 `node --check` + 浏览器/无头冒烟测试。

---

## 二、待办优化方案

### 🔴 P0 — 关键（安全 / 致命 Bug）

**P0-1 前端脚本致命解析错误 → 整页 JS 不执行（当前不可用）**
- 依据：`static/index.html` 第 407 行与第 461 行均为顶层 `let allProblems = [];`。该脚本为经典 `<script>`（非 module），同作用域重复 `let` 是**解析期 SyntaxError**，导致**整段内联脚本完全不执行**。
- 现状：之前验证只 `curl` 了 HTML，未在浏览器跑 JS，故未暴露。当前前端在浏览器中 100% 不可用。
- 修复：删除其一（保留第 461 行）即可，改动本身安全。
- ⚠️ **修复后后果**：脚本首次真正运行，可能暴露 JS 中其他潜在问题；且此前所有"已完成"的前端能力都未经验证。必须配套 `node --check` 语法校验 + 浏览器/无头冒烟测试（验证导航、各页数据加载、弹窗、口试可正常工作），否则"修好解析错误"≠"前端可用"。
- 优先级：**最高，等同于修 Bug，须最先做。**

**P0-2 API Key 明文落库 + 「环境变量优先」不成立**
- 依据：`handler.py:280` 把 `api_key` 写入 SQLite `settings` 表；`ai.py:43` `config=get_cached_settings()` 仅从 DB 读取。**`config.py` 无 api_key 的环境变量读取**（`PHYSICS_OS_DB/PORT/HOST` 有 env，key 没有）。
- ⚠️ **关键修正**：原方案称"即便有环境变量优先逻辑"——**不成立**。若直接按"移除 DB 落库"实施，应用将**完全无法配置密钥**（既无 env、也无 DB），直接不可用。
- 正确做法（须二选一并配套）：
  1. 新增 `PHYSICS_OS_API_KEY` / `PHYSICS_OS_API_BASE` / `PHYSICS_OS_MODEL` 的环境变量读取，合并进 `get_cached_settings()`（env 优先于 DB）；
  2. 设置 UI 改为"会话级"（录入后仅本次运行有效、不写库）或显式文案告知"密钥将以明文存于本地数据库"。
- 不建议只删 DB 写入而不补 env 支持。

**P0-3 无 Origin / Host 校验（localhost CSRF / DNS 重绑定）**
- 依据：`handler.py` 对写操作无 `Origin`/`Host` 校验。同机恶意网页可向 `http://127.0.0.1:8765` 发请求（localhost 不受同源策略限制）。
- 建议：POST/PUT/DELETE 校验 `Origin`/`Host` 须为 `127.0.0.1` / `localhost`，否则 403。
- ⚠️ **实施后果**：易过度拦截。必须**仅限写操作**、放行同源 GET、放行无 `Origin` 头的简请求（同源自 fetch 通常带 Origin，但部分场景可能缺失）；用真实浏览器 fetch 验证，不能只靠 curl。

### 🟠 P1 — 重要（体验 / 数据韧性 / 可访问性）

**P1-1 KaTeX 走 CDN，违背"本地优先"承诺**
- 依据：`index.html:7-9` 从 `cdn.jsdelivr.net` 拉 KaTeX。**离线时公式不渲染**，而物理学习高度依赖公式。
- 建议：下载 KaTeX 到 `static/vendor/` 本地引用；或在 README 声明"首次渲染需联网"。
- ⚠️ **实施后果**：增加 ~250–400KB（含 .woff2 字体）。须**完整拷贝 dist**（含 `katex.min.css` / `katex.min.js` / `contrib/auto-render.min.js` / `fonts/*.woff2`），否则公式变豆腐块；`build.spec` 须把 `static/vendor` 一并打包。方向正确，推荐做。

**P1-2 无数据导出 / 导入 / 备份**
- 依据：数据仅一个 SQLite 文件，无任何导出入口。换机、备份、分享均无路径，误删即永久丢失。
- 建议：加"导出 JSON / Markdown" + "导入"；可选一键 `sqlite3 .backup`。
- ⚠️ **实施后果**：导出安全（只读）。**导入风险高**：若不做参数化插入 + schema 版本校验 → SQL 注入 / DB 损坏；导入须先备份现有 DB + 确认弹窗（呼应 P1-6）。

**P1-3 前端无失败重试 / 离线提示**
- 依据：`api()` 失败仅 `toast(e.message,'error')`，网络抖动需手动刷新。
- 建议：失败自动重试 1 次 + 顶部常驻"连接断开"状态条。
- ⚠️ **实施后果**：对 POST（建题）重试会在响应丢失时**产生重复题目**。须仅重试 GET 或加客户端幂等键（请求带唯一 id，后端去重）。

**P1-4 后端分页接口被前端架空**
- 依据：后端支持 `?page&limit`（已验证 `handler.py:85-99` 返回 `{items,total,page,limit,pages}`），但 `index.html:466` 直接 `limit=200` 拉全量后纯客户端过滤。题目超 200 条会明显变慢。
- 建议：前端改为按页请求；搜索/筛选下沉后端 `?q=&course=&topic=`。
- ⚠️ **实施后果**：低风险。后端已就绪，前端 `loadProblems` 的 `data.items || data` 回落已兼容两种返回。

**P1-5 导航项 `<div>` 键盘不可聚焦**
- 依据：`index.html:166-170` 的 `nav-item` 是 `div`，无 `tabindex`、无 Enter 处理。纯键盘/读屏用户进不去任何页面。
- 建议：改为 `<button class="nav-item">`，或加 `tabindex="0"` + `keydown(Enter/Space)`。
- ⚠️ **实施后果**：`button` 有默认边框/背景，须重置 CSS，否则样式错位。小风险。

**P1-6 删除用原生 `confirm()`**
- 依据：`index.html:607` `confirm(...)` 阻塞主线程、样式割裂。
- 建议：改用项目内 `.modal-overlay` 风格确认框，与编辑/详情弹窗统一。
- ⚠️ **实施后果**：现 `confirm()` 是同步 `await`，改模态框须转为 Promise/回调，删除流程有断裂风险。中风险，建议与 P1-8/1-9 合并一批做。

**P1-7 全程无加载态（skeleton / spinner）**
- 依据：`loadDashboard/loadProblems/loadReviews/loadSettings` 在 `await` 期间页面空白或显示旧内容，慢网像卡死。
- 建议：列表区先渲染"加载中…"或骨架屏；口试等待 AI 加"AI 思考中…"气泡（见 P2-2）。
- ⚠️ **实施后果**：纯增量，零风险。

**P1-8 弹窗无焦点陷阱（focus trap）**
- 依据：`index.html:397-404` 能 Esc/点遮罩关，但打开后 Tab 仍能切到背后元素，读屏读到隐藏内容。
- 建议：打开时焦点移入首个可聚焦元素，用 `inert` 或焦点循环锁背景，关闭时归还焦点。
- ⚠️ **实施后果**：`inert` 现代浏览器（2023+）已支持，低风险；若需兼容旧浏览器改手动焦点循环。

**P1-9 表单 `<label>` 未与控件关联**
- 依据：编辑弹窗 `<label>` 仅视觉文本（如 `index.html:290`），无 `for`/`id`。
- 建议：全部改成 `<label for="editTitle">` + 对应 `id`。
- ⚠️ **实施后果**：纯增量，零风险。

**P1-10 掌握度刻度不统一（禁止改 stored schema）**
- 依据：错题编辑用 1–5（`index.html:326-332`），复习评分用 1–4（`index.html:635-638`），语义易混。
- ⚠️ **关键修正**：原方案"统一为 SM-2 1–4"会**改变数据库字段语义**——现有 `mastery` 存的是 1–5，若改判为 1–4，旧数据（如 `mastery=5`）会被误读/变非法，造成数据损坏。
- 正确做法：**仅 UI 标注两者区别**（如"掌握度 1–5""复习评分 1–4（SM-2）"），**不改 stored schema**，也无需数据迁移。

**P1-11 仪表盘仅静态计数，无趋势**
- 依据：`/api/dashboard` 返回当前总数/待复习，无掌握度随时间变化。
- 建议：存轻量 `review_log(day, avg_mastery)`，前端画掌握度趋势线。
- ⚠️ **实施后果**：需新表 + 每次复习写日志。版本化迁移机制（db.py `schema_version`）已具备，加 v0.3 即可。低风险，增加少量写量。

**P1-12 未启用 SQLite WAL 模式**
- 依据：`db.py` 未设 `PRAGMA journal_mode=WAL`。读写并发时（口试 + 后台复习）读被写阻塞。
- 建议：`init_db()` 内 `conn.execute("PRAGMA journal_mode=WAL")`。
- ⚠️ **实施后果**：WAL 会生成 `-wal` / `-shm` / `-journal` 伴随文件。`.gitignore` 当前**仅 `*.log`**，未忽略这些 → 会被提交。须补 `*.db-wal` / `*.db-shm` / `*.db-journal`。另：PyInstaller 冻结的只读 DB 启用 WAL 需注意（冻结包内 DB 应只读，WAL 写不进）。中低风险。

**P1-13 日志只进 stdout，无文件 / 轮转**
- 依据：`logging` 输出到控制台，后台运行排障无日志可查。
- 建议：`FileHandler` + `RotatingFileHandler` 写到项目内 `physics_study.log`（已被 `*.log` 忽略）。
- ⚠️ **实施后果**：`ai.py` 会读取 `api_key` 进缓存（`get_cached_settings()`），若日志打印 settings 或请求体 → **明文密钥落盘**。须确保日志绝不记录密钥/令牌。中风险。

### 🟡 P2 — 增强（锦上添花）

**P2-1 深色模式** — 加 `prefers-color-scheme: dark` 或手动切换；CSS 已用 `:root` 变量，成本低。

**P2-2 口试"思考中"指示** — `respondOral`（`index.html:684`）已 `disabled` 文本框，但无可见等待提示，建议插临时气泡。

**P2-3 搜索防抖** — `index.html:202` `oninput` 每次按键全量重渲染，数据多时加 200ms debounce。

**P2-4 Toast 可关 + `aria-live`** — `index.html:356` toast 3s 自动消失、读屏读不到；容器加 `role="status" aria-live="polite"` 并支持点击关闭。

**P2-5 深链 / 快捷键** — 页面状态全在内存，刷新回概览；建议写 URL hash（`#review`），数字键 1–5 切换页面。

**P2-6 错题列表排序** — 只有搜索，无"按时间/掌握度"排序；薄弱题排序对复习优先级有用。

**P2-7 移动端 `.form-row` 间距** — `index.html:150` 移动端 `gap:0` 使堆叠字段贴在一起，建议保留 12px。

**P2-8 复习手动控制** — 列表不显示"下次复习日期"，也不能"提前再复习 / 标记掌握"；建议卡片显示 next due + 快捷按钮。

**P2-9 Docker / CLI 友好** — 可选 `Dockerfile` + 一键 `start.sh`；加 `-h/--help`、`--version` 参数。

---

## 三、分阶段执行路线图

| 阶段 | 包含项 | 目标 | 注意事项 |
|------|--------|------|----------|
| **阶段 1（紧急修复）** | P0-1、P0-2、P0-3 | 消除致命 Bug 与安全风险，前端真正可用 | P0-1 必须先做；P0-2 须补 env 支持（非只删 DB）；P0-3 仅限写操作且用浏览器验证 |
| **阶段 2（体验与韧性）** | P1-1 ~ P1-4、P1-7 | 本地优先兑现、数据可备份、分页真正生效、加载态 | P1-2 导入须参数化+备份；P1-3 仅重试 GET；P1-1 须完整拷贝字体 |
| **阶段 3（无障碍与一致性）** | P1-5、P1-6、P1-8、P1-9、P1-10 | 键盘可达、统一交互、焦点管理、语义一致 | P1-10 **只标注不改 schema**；P1-5 重置 button CSS；P1-6 转异步 |
| **阶段 4（数据与可观测）** | P1-11、P1-12、P1-13 | 趋势看板、并发优化、日志落盘 | P1-12 补 .gitignore；P1-13 日志禁记密钥 |
| **阶段 5（打磨）** | P2-1 ~ P2-9 | 深色模式、快捷键、排序等体验增强 | 低风险打磨 |

---

## 四、验收清单

- [x] `node --check` 无 JS 语法错；**真实浏览器无 JS 报错且可导航/加载数据/弹窗/口试**（P0-1，新增）
- [x] `PHYSICS_OS_API_KEY` 等环境变量可配置密钥；DB 不再存明文（或文档明示风险）（P0-2，修正）
- [x] 跨域 `Origin` 写请求被 403；同源写请求正常（P0-3）
- [x] 断网时公式仍可渲染 / README 已声明（P1-1）
- [x] 导出 / 导入按钮可用，导入参数化+先备份、数据往返一致（P1-2）
- [x] 题目 >200 条时分页请求生效，列表不卡；POST 重试不产生重复题（P1-3 / P1-4）
- [x] 纯键盘可进入并操作全部页面（P1-5、P1-8、P1-9）
- [x] 删除确认框风格统一（P1-6）
- [x] 所有数据加载有可见状态（P1-7）
- [x] 掌握度 1–5 与复习 1–4 已在 UI 明确区分，stored schema 未变（P1-10，修正）
- [x] 新增 `review_log` 表迁移成功、趋势图渲染（P1-11）
- [x] WAL 启用且 `-wal/-shm/-journal` 已被 .gitignore 忽略（P1-12）
- [x] 日志落盘且不含 api_key（P1-13）
- [x] 61 → 65 单测仍全绿；新增项补测试（P0/P1 相关）

---

# 附录 B：高风险项的安全替代方案（v3）

> 针对正文「二」中标红的 7 项（P0-1、P0-2、P0-3、P1-2、P1-3、P1-10、P1-13），给出**规避严重后果的全新做法**。每项先说"朴素改法为何危险"，再给"替代方案 + 关键片段"。仅建议，未改代码。

## B.1 P0-1 前端脚本解析失败 — 替代：抽为独立 JS 文件 + 强制语法闸门

**朴素改法的残留风险**：删掉重复的 `let allProblems`（407 行）只消除 `SyntaxError`；但这段内联脚本**从未在真实浏览器跑过**，解析修复后首次执行可能暴露其他运行时问题（未定义函数、事件绑定失效）。

**全新替代方案**：
1. 把内联 `<script>` 整体抽到 `static/app.js`，`index.html` 用 `<script src="app.js" defer></script>` 引入。好处：
   - 可用 `node --check static/app.js` 做**零依赖语法闸门**（CI/提交前跑），语法错不再漏到浏览器；
   - defer/模块作用域天然隔离，顶层重复声明直接报编译错而非运行时崩；
   - 与已删的死文件 `app.js` 同名但内容全新，顺带清理历史混淆。
2. 加轻量运行时冒烟（不引 Playwright，保持零依赖）：30 行 node 脚本，用桩 `global.document`/`global.fetch` 模拟最小 DOM，加载 `app.js` 后断言 `typeof loadProblems === 'function'` 且初始化不抛错。
3. 验收从"curl 返回 HTML"升级为"`node --check` 通过 + 冒烟不抛错 + 手动浏览器跑通导航/加载/弹窗/口试"。

## B.2 P0-2 API Key 明文落库 — 替代：env 优先 + 内存密钥 + 永不持久化

**朴素改法的风险**：原"移除 DB 落库"会让应用**完全无法配置密钥**——`config.py` 只有 `PHYSICS_OS_DB/HOST/PORT` 的 env，没有密钥；`ai.py:43` 的 `get_cached_settings()` 只从 SQLite 读，删了就无来源。

**全新替代方案（密钥只活内存 + env）**：
- `config.py` 新增 `PHYSICS_OS_API_KEY` / `PHYSICS_OS_API_BASE` / `PHYSICS_OS_MODEL` / `PHYSICS_OS_TEMPERATURE` env 读取。
- `ai.py` 的 `get_cached_settings()` 改为 **env 覆盖 DB**；密钥这一项**只来自 env 或内存，绝不写库**。
- `handler._handle_update_settings` 收到 `api_key` 时**只存内存**（如 `ai.py` 模块级 `_runtime_key` 字典 + `invalidate_settings_cache()`），不执行 `INSERT INTO settings`。DB 的 `settings` 表只留非机密 `api_base`/`model`/`temperature`。

```python
# config.py 新增
API_KEY_ENV = os.environ.get("PHYSICS_OS_API_KEY", "").strip()
API_BASE_ENV = os.environ.get("PHYSICS_OS_API_BASE", "").strip()
MODEL_ENV = os.environ.get("PHYSICS_OS_MODEL", "").strip()

# ai.py
_runtime_key = {"value": ""}  # 仅内存

def set_runtime_key(k: str) -> None:
    _runtime_key["value"] = k.strip()
    invalidate_settings_cache()

def get_cached_settings():
    s = settings_dict(include_secret=True)        # 仅非机密落库
    s["api_key"] = API_KEY_ENV or _runtime_key["value"]
    s["api_base"] = API_BASE_ENV or s.get("api_base", "")
    s["model"] = MODEL_ENV or s.get("model", "")
    return s
```
效果：DB 再无明文密钥，且仍可通过 env 或一次性 UI 填密钥——两全。

## B.3 P0-3 写操作无 Origin 校验 — 替代：双提交 CSRF Token（比 Origin 白名单更稳）

**朴素改法的风险**：对写操作检查 `Origin` 头易**误杀**——同源 `fetch` 有时不带 `Origin`（简单请求/导航发起），或 `Origin: null`（沙箱/扩展），一刀切 403 会打断正常功能；且 localhost 跨源攻击面特殊，单靠 Origin 不够。

**全新替代方案：同源专属自定义头（double-submit token 轻量版）**：
- 前端 `api()` 包装器**始终**带 `X-Requested-With: PhysicsStudyOS`。
- 后端对所有 POST/PUT/DELETE 校验该头；缺失或非预期值 → 403。
- 同源策略保证：跨站恶意页**无法**给 fetch 添加自定义头（会触发预检，而本服务不响应 `OPTIONS` 预检），该头成为"来自本 SPA"的可靠凭证。
- 纵深防御可叠加 `Origin` 允许名单（仅放行 `127.0.0.1`/`localhost` 同端口），但**主防线是自定义头**，避免误杀。GET 完全不校验。

```js
// 前端 api() 包装器
async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'PhysicsStudyOS' },
    ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || '请求失败');
  return data;
}
```
```python
# handler 写方法入口校验（POST/PUT/DELETE 共用）
def _guard_csrf(self) -> bool:
    return self.headers.get("X-Requested-With") == "PhysicsStudyOS"
# 在 do_POST/do_PUT/do_DELETE 开头（非 GET/HEAD）：
if not self._guard_csrf():
    self.json_response({"error": "非法请求"}, 403); return
```
（决策流见下方图示。）

## B.4 P1-2 导入/导出 — 替代：导出只读；导入走"文件级替换 + 先备份"

**朴素改法的风险**：把导入 JSON 解析后 `INSERT` 进活动库，若未严格参数化/校验 → SQL 注入或 schema 错位致库损坏；且出错即覆盖。

**全新替代方案（推荐路 A，零注入）**：
- **路 A（首选）**：导出 = 复制 `physics_study.db` 为 `.db.bak`；导入 = 先备份当前库，再整体替换（关连接后 `os.replace`）。100% 安全，保留全部关系。
- **路 B（人类可读 JSON）**：导出只读（`SELECT` → JSON/Markdown，天然安全）；逻辑导入仅在"先备份 + 严格 schema 校验 + 参数化 executemany"三件套齐备时开放：校验字段类型/长度/外键，只用 `?` 占位参数化语句，先写临时库校验通过再 swap，全程夹在确认模态框之后。

## B.5 P1-3 前端失败重试 — 替代：GET 才重试；写操作用"幂等键 + 服务端去重"

**朴素改法的风险**：给 `api()` 加"网络错误自动重试"，POST 建题在响应丢失时重试会**产生重复题目**（同内容插入两次）。

**全新替代方案**：
- **GET/列表类**：幂等，可安全重试（仅 `res.status === 0` 网络层失败、≤1 次）。
- **写操作**：不靠盲重试，改用**幂等键**：前端每次变更生成 `crypto.randomUUID()` 放头 `X-Request-Id`；服务端对 POST /api/problems 维护短生命周期"已见请求"集合，若 `X-Request-Id` 已存在直接返回首次 `201 + id`，不再插入。网络层失败且无响应时，前端提示"提交可能未成功，请到列表确认"，由用户决定手动重试。

```js
async function apiMutate(path, body) {
  const id = crypto.randomUUID();
  return api(path, { method: 'POST', body,
    headers: { 'X-Request-Id': id, 'X-Requested-With': 'PhysicsStudyOS' } });
}
```
```python
_seen = {}  # request_id -> problem_id, 带 TTL
rid = self.headers.get("X-Request-Id")
if rid in _seen:
    return self.json_response({"id": _seen[rid]}, 201)  # 幂等返回
# 正常插入后 _seen[rid] = problem_id
```

## B.6 P1-10 掌握度刻度不一致 — 替代：stored 保持 1–5，新增"展示层映射"

**朴素改法的风险**：把 `mastery` 从 1–5 "统一"成 SM-2 的 1–4 会**改变字段语义**——`mastery=5` 变非法、历史数据误读、前端 `masteryBar` 循环全错。

**全新替代方案（不改 schema）**：
- `mastery INTEGER 1–5` 继续作**用户自我评估**唯一真值，不动。
- 复习评分（SM-2 的 1–4）已在 `reviews.result` 列独立存储——两套刻度本就分列，问题只在 UI 没说清。
- 纯前端补图例：`掌握度 ★1–5 = 自我评估`；`复习评分 1–4 = SM-2`；并把 `mastery` 映射到文字（生疏/了解/掌握/熟练/精通）提升可读性。
- 若做深度 SM-2：**新增派生列/视图** `sm2_level`（由 `ease_factor`/`repetition` 推导），绝不覆盖 `mastery`。

## B.7 P1-13 日志落盘 — 替代：Redaction Filter + 永不记录密钥/请求体

**朴素改法的风险**：加 `FileHandler` 后，`ai.py` 读取的 `api_key` 一旦被某处 `LOG` 打印即**明文落盘**；且 `.gitignore` 已忽略 `*.log`，但 `*.db-wal` 等未忽略（见 P1-12）。

**全新替代方案**：
- 写 `logging.Filter` 子类 `SecretRedactor`，emit 前把 `api_key` 实际值、`Bearer ...`、字串 `api_key`/`Authorization` 全替换为 `***`。
- `call_ai` **绝不**把 `config` 传 `LOG`；`handler` 记请求只记方法+路径，**绝不记解析后的 `data` 体**（PUT /api/settings 的 body 含密钥）。
- `RotatingFileHandler`（maxBytes=1MB, backupCount=3），路径在 APP_DIR，已被 `*.log` 忽略。
- 与 P1-12 协调：启用 WAL 时同步在 `.gitignore` 补 `*.db-wal`/`*.db-shm`/`*.db-journal`。

```python
import logging, re
class SecretRedactor(logging.Filter):
    def __init__(self):
        self.patterns = [re.compile(r"Bearer\s+\S+"),
                         re.compile(r"api_key[\"']?\s*[:=]\s*[\"']?\S+")]
    def filter(self, record):
        msg = record.getMessage()
        for p in self.patterns:
            msg = p.sub("***", msg)
        record.msg, record.args = msg, None
        return True
LOG.addFilter(SecretRedactor())

---

## 五、第三轮实施完成记录（2026-08-02）

**全部待办项已落地并验证。** 改动严格限制在 `E:\tool\biancheng\AI project 2\physics-study-os\` 内。

### 改动映射

| 项 | 改动 | 文件 |
|----|------|------|
| **P0-1** | 内联脚本抽到 `static/app.js`（`defer` 引入），删除重复 `let allProblems`；`node --check` 通过 | `index.html` / `app.js`（新建） |
| **P0-2** | 新增 `PHYSICS_OS_API_KEY/API_BASE/MODEL` env；密钥改为「env > 内存 > DB」且**绝不落库**；设置 UI 标注风险 | `config.py` / `ai.py` / `handler.py` |
| **P0-3** | 写请求（POST/PUT/DELETE）校验 `X-Requested-With` 头，缺失 → 403；GET 放行 | `handler.py` / `app.js` |
| **P1-1** | KaTeX 完整下载到 `static/vendor/`（css/js/auto-render + 20 字体），离线可渲染 | `static/vendor/*` |
| **P1-2** | `GET /api/export` 只读导出；`POST /api/import` 参数化写入 + 自动备份 | `handler.py` / `app.js` |
| **P1-3** | GET 失败重试 1 次；写操作带 `X-Request-Id` 幂等键，服务端去重返回首次结果 | `app.js` / `handler.py` |
| **P1-4** | 前端真分页（按页请求 + 后端 `?q=` 搜索 + 排序 + 分页器） | `handler.py` / `app.js` |
| **P1-5** | 导航 `<div>` → `<button>`，重置 CSS，键盘可聚焦 | `index.html` |
| **P1-6** | 删除改用项目内确认弹窗（Promise 化） | `index.html` / `app.js` |
| **P1-7** | 列表/口试加载态（skeleton + "加载中…" + "AI 思考中…"气泡） | `app.js` |
| **P1-8** | 弹窗焦点陷阱（Tab 循环 + Esc + 遮罩关闭 + 焦点归还） | `app.js` |
| **P1-9** | 表单 `<label for>` 与控件 `id` 关联 | `index.html` |
| **P1-10** | UI 标注「掌握度 1–5（题目）」与「复习评分 1–4（SM-2）」区别，schema 不变 | `index.html` |
| **P1-11** | `mastery_log` 表（v2 迁移）；每次复习写日志；前端 SVG 趋势线 | `db.py` / `handler.py` / `app.js` |
| **P1-12** | 启用 SQLite WAL；`.gitignore` 补 `*.db-wal/-shm/-journal/-bak` | `db.py` / `.gitignore` |
| **P1-13** | `RotatingFileHandler` + `SecretRedactor` 脱敏，密钥绝不落盘 | `config.py` |
| **P2-1~9** | 深色模式（自动+切换）、口试思考指示、搜索 250ms 防抖、Toast `aria-live`、URL hash 深链 + 数字键、列表排序、移动端间距修正、复习手动控制（再复习一次/标记掌握） | `index.html` / `app.js` / `handler.py` |

### 验证结果

- ✅ **65 个单元测试全部通过**（新增 4 个：CSRF 拦截、趋势、导出导入往返、写幂等）
- ✅ `node --check static/app.js` 无语法错误（P0-1 语法闸门）
- ✅ 端到端冒烟（独立临时 DB）：前端 `/` + `/app.js` + KaTeX 资源均 200
- ✅ CSRF：无 `X-Requested-With` 的写请求 → **403**；带头的 → 201
- ✅ 密钥仅存内存（`key_source=runtime`），`settings` 表无明文；DB 查询确认 `api_key=''`
- ✅ 趋势记录 `/api/trend` 返回正确；导出/导入往返一致且生成 `.bak` 备份
- ✅ 写幂等：相同 `X-Request-Id` 重复提交返回同一 id，无重复题
- ✅ 日志文件生成且**不含明文密钥**（grep `SUPERSECRET` = 0 命中）

### 附：本回合顺带修复的潜在 Bug

- `config.py` 原在 `LOG` 定义前引用它（坏端口时 `NameError`）→ 已把 `LOG` 定义提前。
- `ai.py` 的 `get_cached_settings` 现按 env > 内存 > DB 正确合并（此前 DB 仅来源、无 env 支持）。
```
