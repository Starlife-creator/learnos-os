# LearnOS — 个人学习 OS

> 本地优先、零第三方依赖的个人学习辅助工具。
> 记录尝试 → 分级提示 → 主动回忆 → 间隔复习 → AI 口试。

## 功能

- **多学科支持**：物理 / 化学 / 数学内置学科，可在设置页「学科管理」直接新增/删除自建学科（支持中文显示名）；知识图谱、题库、错题、复习按学科隔离
- **错题管理**：记录题目、自己的尝试、错误类型、掌握度、拍照附件；卡片/表格双视图（列排序）+ 保存的筛选器（Dataview 式自定义视图）
- **分级 AI 提示**：不给答案，逐步引导——概念检查 → 方向指引 → 解题框架 → 完整解析（AI 提示默认缓存，复读不再调用，可在设置中关闭）；口试与提示自动注入学习者画像，追问偏向薄弱区
- **知识图谱**：概念与概念关联、题目-概念绑定、概念掌握进度可视化（`concept_map.html` 独立页）；概念别名 + 未链接提及扫描（错题文本中出现但未绑定的概念一键确认绑定）
- **题库练习**：内置三学科种子题库，按单元练习并记录作答
- **间隔复习**：FSRS-6 优先（内置 vendor 实现），不可用时自动降级 SM-2；目标保持率可调、支持逾期顺延；队列按优先级排序（逾期久 > 掌握度低 > 带漏点）+ 每日上限防爆量 + CMRR 式最优保持率估算
- **记忆校准（北极星指标）**：周期报告第三个 tab 对比「实际保持率」与 FSRS 目标保持率，给出校准缺口与可执行建议（缺口为正 → 可放宽间隔；为负 → 先减新题量）；按时/逾期分组统计，样本不足时只给数字不给结论
- **AI 口试 / 费曼自评**：围绕一个知识点五轮追问，检验真正的理解
- **统一学习队列**：今日行动升级为按优先级逐项推进的学习队列（复习 → 薄弱口试 → 错题巩固 → 题库）
- **今日学习计划**：概览页基于规则引擎（离线可用）生成按优先级排序的行动建议，含预计用时；配置 AI 后可选合成自然语言计划
- **学习打卡**：概览页记录每日学习时长/备注，展示连续天数与累计时长；可导出无答案进度包（仅聚合指标，不含任何题目/答案）安全分享给学习小组
- **全局搜索**：Ctrl+K 跨错题 / 概念 / 题库 / 资料库直达
- **RAG 资料库**：导入教材/课件/笔记，AI 提示与解答优先引用你的资料（支持误删撤销）
- **资料导入向导**：上传 md/txt 教材或试卷（上传即落盘至 uploads/，上限 100MB）或选已摄取文档，AI 按配置的模型上下文自动分段、SSE 流式进度、断点续跑全覆盖——草稿预览、勾选确认后才写入；无 AI 时按标题层级降级提取概念
- **试卷模式**：组卷、模拟测试、整体备考就绪度
- **游戏化**：连续学习天数等激励
- **仪表盘**：掌握度统计、薄弱知识点、趋势分析、学习者画像；AI 健康度卡片显示缓存命中率
- **快捷键**：Ctrl+K 全局搜索；g + d/b/p/r/o/m/e/h/s 跳转各页
- **LaTeX 渲染**：本地 KaTeX（离线），支持 `$...$` 和 `$$...$$`
- **PWA**：manifest + Service Worker，可安装、弱网可用
- **双语**：中 / 英界面切换

## 快速开始

```bash
# 克隆项目
cd learnos-os

# 启动（自动打开浏览器）
python app.py

# 或指定端口
set LEARNOS_PORT=9000 && python app.py
```

**Windows 一键启动**：直接双击 `start.bat`（自动校验 Python 3.11+、运行启动自检、启动服务并自动打开浏览器）。

打开 `http://127.0.0.1:8765` 即可使用。

## 配置

在「设置」页面配置 AI 接口（兼容 OpenAI API 格式）：

| 配置项 | 环境变量 | 默认值 |
|--------|---------|--------|
| API 地址 | `LEARNOS_API_BASE` | `https://api.openai.com/v1` |
| API Key | `LEARNOS_API_KEY` | 空（环境变量 / 加密密钥库 / 内存，绝不写入数据库） |
| 模型名称 | `LEARNOS_MODEL` | 空 |
| 轻量模型 fast | - | 空（可选的提示档专用模型，优先于默认模型） |
| 推理模型 heavy | - | 空（可选的深度推理档专用模型，优先于默认模型） |
| 视觉模型 vision | - | 空（可选，拍照识题 / OCR 用） |
| Temperature | - | `0.3`（范围 0–2） |
| 上下文窗口 | - | `32000`（范围 4000–1000000，决定资料导入的分段大小） |
| 允许本地 AI 端点 | - | 开启（Ollama 等本地/内网端点需要，SSRF 防护开关） |
| 默认学科 | - | physics |
| 提示缓存 | - | 开启（关闭后 AI 提示不再落库） |
| 每日复习上限 | - | 0 = 不限（超出按优先级顺延） |
| FSRS 目标保持率 | - | `0.9`（范围 0.75–0.97，设置页 FSRS 卡可调） |
| 密钥库主密码 | - | 空（设置后加密保存 API Key 至 `data/keys.enc`） |
| 数据库路径 | `LEARNOS_DB` | 项目目录下 `learnos.db` |
| 监听地址 | `LEARNOS_HOST` | `127.0.0.1` |
| 监听端口 | `LEARNOS_PORT` | `8765` |
| 自动打开浏览器 | `LEARNOS_NO_BROWSER` | 开启（设为 1 则启动时不自动打开浏览器） |

> **密钥安全**：API Key 仅来自环境变量、AES-GCM 加密密钥库（`data/keys.enc`，可选）或本次运行内存（UI 录入后重启失效），**永远不会以明文写入数据库文件**。设置页会显示当前密钥来源（environment / keyfile / runtime / none）。

### 数据备份与迁移

- **一键备份/还原**：设置页「一键备份」导出全库 JSON（覆盖全部 20 张业务表，含题库错题建档/作答记录/学科注册/打卡/评分历史/游戏化等），带 sha256 完整性校验；「还原」先自动备份现库再重建回填。
- **导出**：概览页「导出数据」生成 `learnos_YYYY-MM-DD.json`（题目 + 提示 + 复习记录）。
- **导入**：概览页「导入数据」会**先自动备份**当前数据库，再以参数化写入导入，不会造成注入或数据损坏。导入前会弹出确认框。

### 安全

- 所有写请求（POST/PUT/DELETE）需携带 `X-Requested-With` 头，缺失即 403——防范同机恶意网页对 localhost 的跨站调用；暴露模式（`LEARNOS_HOST` 非回环）下还要求 `Authorization: Bearer <LEARNOS_API_TOKEN>`。
- 全库导出/备份/还原端点额外要求**一次性导出挑战令牌**：同源前端每次导出/还原前 `POST /api/export/challenge` 取一个 60s 内单次有效、绑定客户端 IP 的 HMAC 签名令牌（用后即焚，防重放）；`EXPORT_TOKEN` 仅作服务端签名密钥，**不再随 `/api/bootstrap` 回显**。跨源网页既读不到也无法伪造；失败尝试计入限流。
- 所有响应携带 Content-Security-Policy 头：script-src 收紧为仅同源（无内联脚本），阻断外部脚本加载；AI 出站端点做协议白名单 + 重定向拦截 + 本地端点开关（SSRF 防护）。
- SQLite 启用 WAL 模式提升并发读写；日志写入 `learnos.log`（滚动 1MB × 3，自动脱敏密钥）；破坏性操作（删题/清空学科/导入/还原/备份导出）追加最小审计到 `data/audit.log`（IP + 时间 + 动作，不记敏感内容）。
- AI 生成内容（变式题 / 标签 / 拍照识题 / 口述卡片）一律走「草稿 → 用户确认」流程，不静默落库；AI 重接口（打标签/视觉识别/口试/变式/资料分析/评分）按 IP 滑动窗口限流（heavy 档 10 次/60s，fast 档 40 次/60s，可经 `LEARNOS_AI_MAX_HEAVY` / `LEARNOS_AI_MAX_FAST` 调），超限返 429 不消耗外部 API，限流器异常自动放行（fail-open）。

#### 安全部署矩阵（网络暴露相关环境变量）

| 环境变量 | 作用 | 默认 | 备注 |
|---------|------|------|------|
| `LEARNOS_HOST` | 监听地址，**决定是否暴露** | `127.0.0.1` | 回环 = 仅本机；`0.0.0.0`/局域网 IP = 对网络开放 |
| `LEARNOS_ALLOW_LAN` | 仅抑制「暴露但未显式放行」启动警告 | 空 | 不改变任何鉴权行为 |
| `LEARNOS_API_TOKEN` | 暴露态写鉴权 Bearer 令牌 | 空 | 暴露模式且为空 → **启动即拒绝**（绝不静默回退无认证） |
| `LEARNOS_EXPORT_TOKEN` | 导出挑战 HMAC 签名密钥 | 随机生成 | 设置可跨重启稳定；缺失则每次启动换新（在途导出会失效，属预期） |
| `LEARNOS_CHALLENGE_TTL` | 导出挑战有效期（秒） | `60` | 单次有效，用后即焚 |

> **快速安全暴露到局域网**：`set LEARNOS_HOST=0.0.0.0 && set LEARNOS_API_TOKEN=<用 python -c "import secrets;print(secrets.token_hex(32))" 生成>` 后启动。不带 token 启动会被拒绝，这是刻意的 fail-closed 设计。

### 明确不做（非目标）

以下能力**刻意不实现**，理由见 [ADR 0005](docs/decisions/0005-explicit-non-goals.md)；将来要做请先改决策再动手：

- **插件 / MCP 机制**：动态加载本地 `plugin.py` 等于给单机应用开一个任意代码执行口，而当前没有任何消费者。
- **学科渲染配置 API**：前端渲染是写死的样式，这个 API 与 `list_subjects()` 的学科名/图标构成双份事实来源，无人调用即会腐烂。
- **动态 PWA 清单**：`index.html` 直接引用静态 `static/manifest.json`，另开一个接口供同一份文件是纯粹的第二事实来源。

`/api/material/cards` 不属于此类——它在学习页有真实入口（选中文本 → 卡片草稿 → 确认落库）。

## 项目结构

```
learnos-os/
  app.py           # 主入口：启动服务器
  config.py        # 全局配置、路径常量、Schema、日志脱敏
  db.py            # 数据访问层：SQLite 连接、查询、迁移
  ai.py            # AI 应用层：对外入口（标签/变式题/出题/审题/评分）+ 三层再导出
  ai_client.py     # AI 传输层：结果缓存、设置与密钥、HTTP 调用、模型预设
  ai_prompts.py    # AI 提示词层：学科档案、阶段提示词、结构化 schema
  ai_fallback.py   # AI 降级层：断网/未配置时的本地启发式提示与产出
  review.py        # SM-2 间隔复习算法
  fsrs_bridge.py   # FSRS-6 调度桥接（vendor 优先，SM-2 降级）
  scheduler.py     # 统一复习调度入口（题目/闪卡共用；FSRS 全权，SM-2 支做逾期顺延）
  mastery.py       # 掌握度单一写入口（题目 1-5 级 ↔ 0-1 换算 + mastery_events 落事件）
  graph.py         # 知识图谱（概念/关联/进度）
  bank.py          # 题库（单元/作答）
  cards.py         # 知识卡
  learn.py         # 统一学习队列 / 下一步
  rag.py           # RAG 文档与分块
  material.py      # 资料导入向导（教材/试卷 → 图谱/题库/试卷草稿提取）
  reports.py       # 报告/统计领域层：仪表盘、周期报告、趋势与分析（无 HTTP 知识）
  problems.py      # 错题领域层：CRUD、分页检索、查重、批量与导入（无 HTTP 知识）
  review_session.py # 复习会话领域层：队列优先级/交错/每日上限、今日小结、完成复习写回
  hints.py         # 提示领域层：A6 诊断门与提示缓存读写
  exports.py       # 导出领域层：JSON 快照、Anki CSV、复习日程 ICS 的装配与渲染
  domain_errors.py # 领域层 4xx 异常基类（DomainError：message + status）
  subjects.py      # 学科领域层：学科总览/新增/按 scope 级联删除、全局搜索
  metrics.py       # 运维指标采集（ai_telemetry 聚合：token、缓存、p95、最近 provider 错误）
  handler.py       # HTTP 路由与核心业务（已按领域拆分）
  handler_base.py / handler_cards.py / handler_learn.py / handler_material.py / handler_oral.py / handler_problems.py / handler_reports.py / handler_reviews.py / handler_social.py  # Handler 领域拆分
  oral.py          # AI 口试 / 费曼自评
  exam.py          # 试卷模式
  auth.py / ratelimit.py / validate.py / errors.py / resp.py  # 鉴权、限流、校验、错误与响应
  gamification.py / social.py / profile.py / ocr.py / keystore.py / backup.py / trash.py / telemetry.py
  interop.py / agent_rules.py
  vendor/fsrs/     # 内置 FSRS-6 纯 Python 实现
  data/            # 三学科种子数据（概念 + 题目）
  docs/decisions/  # 架构决策记录（ADR）
  static/
    index.html     # 前端主页面
    app-*.js       # 前端模块（core/init/dashboard/learn/problems/bank/cards/review/exam/rag/oral/settings）
    concept_map.html  # 知识图谱独立页
    locale/        # 中英文语言包
    vendor/        # 本地 KaTeX（离线可用）
  tests/           # 单元/HTTP/端到端/架构守卫测试
  run_tests.py     # 唯一测试入口
  build.spec       # PyInstaller 打包脚本
  start.bat/stop.bat  # Windows 启停
```

上面的文件清单由 `tests/test_fitness_functions.py` 校验：列出的文件必须存在，根目录的 `.py` 模块必须被列出。

## 测试

```bash
python run_tests.py
```

`run_tests.py` 是**唯一权威入口**（CI 与本地共用），它以 `-t .` 包模式启动测试，
从而加载 `tests/__init__.py` 里的加固 shim（超时下限、临时目录 ACL 修正、日志重定向到 `tests/.tmp/test.log`）。
跑完后若有临时目录删不掉，会在末尾打印残留清单。
请勿改用 `python -m unittest discover -s tests` —— 那样会绕过 shim，
使 16 个文件中 37 处 `timeout=N` 用例在慢机器上随机失败
（该一致性由 `tests/test_fitness_functions.py` 断言守卫）。

覆盖范围：SM-2 / FSRS 算法、AI 提示词构造与降级、数据库 CRUD 与密钥遮蔽、HTTP 端点全路径、知识图谱、题库、RAG、口试、端到端学习循环，以及架构守卫（全局符号遮蔽、路由元数、AI 配额覆盖、备份表完整性、性能预算）。

## 构建

```bash
# 安装 PyInstaller
pip install pyinstaller

# 打包为单文件 exe
pyinstaller build.spec
```

产物在 `dist/LearnOS.exe`。

## 技术栈

- **后端**：Python 标准库（`http.server` + `sqlite3` + `urllib`），零第三方依赖
- **前端**：Vanilla HTML/CSS/JS，模块化 `static/app-*.js` + 本地 KaTeX（离线可用）
- **算法**：FSRS-6（内置）优先，SM-2 降级
- **打包**：PyInstaller

## 学习方法论

1. **记录尝试**：先自己写，哪怕写错——错在哪里比正确答案更有价值
2. **分级提示**：从概念检查到完整解析，逐级递进，AI 不代写作业
3. **主动回忆**：FSRS/SM-2 算法在最佳遗忘点安排复习，强制提取记忆
4. **口试追问**：五轮深度提问，检验你是否能用自己的话解释概念

## 许可

个人学习使用。
