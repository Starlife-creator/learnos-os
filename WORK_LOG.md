# 工作日志（WORK LOG）

> 每次 AI 迭代完成后的落地摘要，按日期追加。对应 `OPTIMIZATION_PLAN_V2.md` 各批次。

---

## 2026-08-13：F2 批次「双语 i18n + AI 英文讲解」（完成）

原则延续：**零新增依赖、零大文件**（locale 纯 JSON，无 i18n 库）。

全量回归：**174 passed + 4 subtests**，`node --check static/app.js` 与图谱内嵌 JS 均通过。

- **词条**：`static/locale/zh-CN.json` / `en-US.json`（223 键，键集强制一致，测试守护 test_locale_consistency）。
- **index.html**：154 处 `data-i18n` / `data-i18n-ph` / `data-i18n-aria`（导航、统计、概览卡片、错题页、复习页、口试页、教材库、exam 页、设置页、modal）；设置页语言下拉 `#langSelect`。
- **app.js**：头部 i18n 核心（`loadLocale`/`t`/`applyI18n`/`setLang`/`refreshForLang`，localStorage `lang` 默认 zh-CN，切换按当前页重渲染）；高频动态文案（加载中/暂无数据/已保存/已删除等）改走 `t()`。
- **concept_map.html**：独立轻量 i18n（内嵌 `_dict`/`t`/`applyGraphI18n`/`bootGraph`），图例/说明/详情标签/添加表单/动态文案全双语，`document.title` 随语言。
- **AI 英文讲解**：`problem_prompt`/`fallback_hint` 增加 `lang` 参数（en 分支：英文 system 提示 + 英文 level 规则 + 英文降级模板）；hint 请求体带 `lang: currentLang()`，handler 透传到 SSE 与非 SSE 路径（test_hint_lang_en_fallback）。
- 测试 +2：test_hint_lang_en_fallback、test_locale_consistency。

---

## 2026-08-12：P2 批次「D4 遗忘曲线 + 图谱语义修复」（全部完成）

原则延续：**零新增依赖、零大文件**（SVG 纯手绘，无 d3/图表库）。

全量回归：**172 passed + 4 subtests**，`node --check static/app.js` 通过。

| # | 项 | 实现 | 验证 |
|---|---|---|---|
| 1 | D4 记忆保持曲线 | `/api/analytics` 加 `forgetting`：FSRS 卡（state>0）按距上次复习天数分 6 桶（0-3/4-7/8-14/15-30/31-60/60+）统计实测平均 R；平均稳定度下算 t=0..30 预测曲线；前端概览页「记忆保持曲线」卡——SVG 网格/预测线/实测绿点/目标保持率虚线（读 localStorage fsrsRetention），零图表库 | test_analytics_forgetting_curve（桶数/平均稳定度/曲线单调性） |
| 2 | 图谱语义修复 | `concept_map.html`：点击概念的「相关错题」原来错误地显示同课程题目（`/api/problems/{id}/related`），改为与先修模式同源的 `/api/graph/problems?concept=<id>`（绑定概念+先修链错题）；无绑定题目但未学习的概念节点改显灰色（原全红误报为「先修不足」） | 纯前端 |

### 说明
- A2 概念图谱后端（种子/传播/绑定/先修告警）与图谱页（SVG 力导向布局、滚轮缩放、拖拽平移、详情面板、添加概念、先修模式跳转）均已在早期批次落地；本批修正其「相关错题」数据语义与掌握度配色。
- F2 双语：单用户中文场景价值低且全量字符串抽离回归风险大，按「工作量无关紧要但价值/风险比不高」暂缓，不列入。

---

## 2026-08-12：P1 批次「零依赖四件套」优化（全部完成）

原则：**不多装依赖、不引大文件**——本批四项全部纯标准库 + SQL + 前端，无任何新增第三方依赖。

全量回归：**171 passed + 4 subtests**，`node --check static/app.js` 通过。

| # | 项 | 实现 | 验证 |
|---|---|---|---|
| 1 | C6 AI 调用遥测 | 新表 `ai_telemetry`（仅元数据：route/model/latency_ms/tokens/ok/error_kind，**不含任何内容**，R4 隐私）；新模块 `telemetry.py`（`record` 写入时顺手清理 >90 天数据，防膨胀；`summary` 近 7 天调用/失败率/均延迟/估算 token/慢路由 TOP3）；`ai.py` 的 `call_ai`/`call_ai_stream` 埋点（route 可选参数，流式 urllib 请求/迭代成功失败 均记录），handler hint/oral/设置测试连接均传 route | test_telemetry（无 AI 降级也计数 ok=0） |
| 2 | D5 学习日志周报 | `Handler._weekly_report()`：周一至今 vs 上周（新增题/复习数/保持率/增量）+ 模板建议句（按保持率 0.7 分档，零 AI 依赖），并入 `/api/dashboard` 单请求 | test_weekly 字段断言 |
| 3 | D6 游戏化防刷 | 新表 `gamification(date PK, reviews, xp)`；XP 按 rating 结算（1→1/2→3/3→8/4→15），复习完成接口挂钩；streak 逐日回推；6 枚本地徽章（首战/7日/30日/百题/单日20/XP500，无排行榜无内购）；`/api/gamification` 端点 | test_gamification（15 XP/首战解锁） |
| 4 | A8 一题多解 | `problems.methods`(JSON 字符串数组) v15 迁移；创建/更新接口收口 `_as_str_list`（只收字符串元素，非法忽略）；详情弹窗「一题多解」区块：展示/添加（prompt）/删除，复习时可换思路重做 | test_methods_roundtrip（覆盖/清空/非法元素） |

### 附注/事故记录
- `_as_str_list` 初始用 `str(v)` 会把 dict 元素转成 `"{'bad': …}"` 落库——改为只接受 `isinstance(v, str)`，杜绝脏数据。
- `telemetry.py` 初版写了冗余 `record_timing` 包装，后合并进 `record(start=…)` 简化。
- PowerShell 不支持 heredoc，批量改 oral.py 的 7 处 `call_ai(` 用临时脚本文件完成（`tests/.tmp/`，已删除）。

### 可选依赖说明
- 本批未引入任何新依赖；`ai.py` 双档（fast/heavy）与 `/api/export?ansi-csv|ics` 均已在前批落地。
- 遥测 token 精确值取自响应 `usage`（OpenAI 兼容）；流式响应无 usage 时记 0，仅作趋势参考。

---

## 2026-08-12：P0 八项「借鉴成熟项目」优化（全部完成）

全量回归：**169 passed + 4 subtests**，`node --check static/app.js` 通过。

| # | 项 | 实现 | 验证 |
|---|---|---|---|
| 1 | FSRS 个性化参数训练 | `fsrs_bridge.py` 扩展：`train_parameters()`（要求 torch/pandas/tqdm + ≥10 条复习，bounds 校验，tmp+os.replace 原子写 `data/fsrs_params.json`）、`train_async()` 后台线程、`reset_parameters()`、`retrievability()` 遗忘预测、`set_desired_retention()`（设置 key `fsrs_desired_retention` 0.75-0.97，默认 0.9）接入 `_scheduler()` 工厂；端点 `/api/fsrs/status`、`/api/fsrs/train|reset|retention`；设置页 FSRS 卡（滑条 + 训练/重置）。**本机无 torch/pandas/tqdm → 降级指引，调度主路径不受影响** | test_fsrs_bridge 8 用例（含缺依赖降级） |
| 2 | 压力指数（PI） | `Handler._pressure_index()`：逾期/今日/明日计数 × 1.5 分钟估时 → 0-100 分（逾期×2 上限 60 + 今日×0.8 上限 30 + 明日×0.3 上限 15），高/中/低分级，并入 `/api/dashboard` | test_dashboard 全绿（含压力字段） |
| 3 | 遗忘预测（R 值） | `_forget_predict()`：due≤明日 的复习 join problems（state/stability/difficulty），逐卡算 R；高风险 R<0.5 / 中风险 0.5-0.7 / 平均 R / 最危险 3 题 | test_dashboard 断言 200 全过 |
| 4 | 反复错题追踪 | `_stubborn_problems()`：同题评分≤2 次数≥2 入「顽固错题」榜（按错次降序、掌握度升序，附再错率），概览页新卡 | test_stubborn_problems（SM-2 答错重置 repetition，判定只依赖错次） |
| 5 | 闪电/翻卡复习 | 复习页「⚡ 闪电复习」全屏翻卡：忘了(1)/记得(4) 一键记入 FSRS，键盘 ←/→，点击翻面，进度+完成统计 | `node --check` |
| 6 | 打印增强（考前自测卷） | 「📝 考前自测卷」按钮：薄弱优先抽 ≤30 题、按知识点交错组卷、**不打印答案/对策**，标注建议时长，作答线留白 | `node --check` |
| 7 | 今日任务清单 | `_today_tasks()`：今日复习量 + 错因专项（TOP 错因 + 3 道最薄弱题）+ 距考 ≤14 天提醒，概览页新卡 | test_dashboard 全绿 |
| 8 | 同知识题隔开 | 复习队列**默认交错**（`_interleave` 贪心轮转，`?mode=plain` 关闭），复习页开关改为默认开（localStorage 语义反转） | test_reviews_interleave_mode 仍过 |

### 附注/事故记录
- `_forget_predict` 初版误取 `r.state/r.stability/r.last_review`（FSRS 状态列在 `problems` 表）→ 500；改为 join problems + reviews 子查询取最近完成时间，3 个 dashboard 用例一次修复。
- 顽固题初版按 `repetition≥2` 筛选失败——SM-2 答错会重置 repetition=0；改为按错次（评分≤2）统计，前端显示同步改用错次。
- 测试误删行已恢复（edit 冗余行导致 `body` 变量丢失）。

---

## 2026-08-12：C7 八项打磨优化（全部完成）

全量回归：**165 passed + 4 subtests**，`node --check static/app.js` 通过。

| # | 项 | 实现 | 验证 |
|---|---|---|---|
| 1 | 每日首次启动自动备份 | `backup.auto_backup_if_due()`：`backups/auto_YYYY-MM-DD_HHMMSS.db`，同日幂等（文件存在即跳过），保留最近 7 份；`app.py:main()` 在 `init_db()` 后调用，失败不阻塞启动（`backup.py`） | test_backup.py 幂等/裁剪用例 |
| 2 | 错因趋势（近 30 天 vs 历史） | `Handler._error_trend()`：近 30 天占比 vs 历史占比 + delta（↑恶化/↓改善/→持平），并入 `/api/dashboard` 单请求 | test_handler 断言字段与「旧题不入近 30 天」 |
| 3 | 相似题查重 | `GET /api/problems/duplicates?content=&topic=&exclude=`：字符 bigram Jaccard（零依赖），同 topic 加权 +0.15，阈值 0.35，最多 5 条；编辑弹窗内容输入防抖 800ms 提示，点击直达详情（`app.js checkDuplicates`） | 端点/单元/空内容用例 |
| 4 | 打印错题集 | 错题页「🖨 打印错题集」按钮：全量拉取（limit 上限放开至 10000 由后端 200 截断前按当前搜索/排序），按掌握度降序渲染到 `#printArea` + `@media print` 隐藏其余；列表接口补充 content/my_attempt/fix_action 字段供打印 | `node --check` |
| 5 | 冲刺倒计时卡 | 概览页新卡：距考试天数 + 未掌握题数 → 每日建议题数；数据取自 `profile.goal`（考试日期/目标分），随 `loadProfile(dash)` 渲染 | 纯前端 |
| 6 | 语音输入 | 编辑弹窗「🎤 语音输入」：`webkitSpeechRecognition`（zh-CN，interimResults）追加到「我的尝试」；不支持浏览器 toast 降级 | 纯前端 |
| 7 | PWA 离线缓存 | `static/sw.js`（静态 cache-first + 版本清理；`/api/` 网络优先、离线回退缓存）、`manifest.json`、`icon-192/512.png`（stdlib 生成）；index.html 注册 + SW 注册（仅 http/https） | test_handler 静态资产 4 项 200 + 内容断言 |
| 8 | SSE 重连 | `getHint` 重构为 `streamOnce()` + 外层重试循环：断流自动重连 ≤2 次（保留已输出内容续传），重连 toast 提示，仍失败则提示「流式连接中断」 | `node --check` |

### 附注/事故记录
- 调试脚本曾漏 `db.DB_PATH` 同步赋值（db 按值绑定）导致指向真实库——INSERT 因 NOT NULL 失败未提交，**零污染**；教训：任何脚本改库路径必须 `config.DB_PATH` 与 `db.DB_PATH` 同步。
- 查重测试初次预期「排除自身后命中自身」逻辑错误，修正为「近亲题」语义。
- `_error_trend` 初版 `dict(rows(...))` 误用（行 dict 键展开），改显式 comprehension。

---

## 2026-08-11：B5/B2/C6 批次（前置背景）

- **B5 一键备份/还原**：`backup.py`（13 表全量 JSON；还原前自动 `.bak`，按 FK 序重建），`GET /api/export/backup`、`POST /api/import/restore`，设置页双按钮。
- **B2 试卷 OCR**：`ocr.py`（pdfminer 文本层 → pypdfium2 渲染 + paddleocr 逐页识别，依赖缺失 ValueError 降级指引）；`GET /api/ocr/probe`、`POST /api/ocr/extract`（工作区路径校验）；教材库页 OCR 卡。
- **C6 七项**：逾期复习顺延（5-20 天减半 / ≥21 天重置）、错因注入提示词、浏览器通知+角标、错因分布图、复习偏好设置、dashboard 单请求合并、BM25 缓存失效。
- **数据修复**：真实库存 v10 → 手动迁移 v14；`settings.api_key` 明文残留清除（密钥出库合规）。