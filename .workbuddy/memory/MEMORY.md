# 项目长期记忆

## 项目约定
- **所有改动和影响严格限制在项目文件夹内**（`E:\tool\biancheng\AI project 1\physics-study-os\`），不修改项目外的任何文件。
- Python 运行时使用受管版本：`C:\Users\Lenovo\.workbuddy\binaries\python\versions\3.13.12\python.exe`
- 项目零第三方依赖原则，尽量只用 Python 标准库。

## 项目概况
- 本地优先的物理学习辅助工具（错题管理 + 三级AI提示 + 间隔复习 + AI口试）
- 后端：模块化结构 (app.py + config.py + db.py + ai.py + review.py + oral.py + handler.py)
- 前端：static/index.html 单页应用 + static/app.js (抽离脚本) + static/vendor/ 本地 KaTeX
- 测试：tests/ 目录，65 个单元测试全部通过
- 复习算法：SM-2 (SuperMemo 2)，含 ease_factor 和 repetition 跟踪
- 构建：build.spec PyInstaller 脚本
- 数据库：SQLite，含版本化迁移机制（schema_version 表，已到 v2 mastery_log）
- 安全：写请求 X-Requested-With 网关；API Key 仅 env/内存、绝不落库；WAL；SecretRedactor 日志脱敏

## 已完成的优化
### 第一轮（2026-08-02）
- P0: 创建 static/index.html 前端页面 ✅
- P1: 创建 tests/ 测试目录（53个测试）✅
- P1: 模块化拆分 app.py ✅
- P2: SM-2算法、_safe_error、AI重试、DB迁移 ✅
- P3: .gitignore、logging、端口冲突、start.bat、build.spec ✅

### 第二轮（2026-08-02）
- P0: hint泄露修复、.gitignore !build.spec、Windows端口检测 ✅
- P1: 响应式CSS、KaTeX渲染、删死文件、分页API、测试隔离、DELETE 404 ✅
- P2: 版本化迁移、设置缓存、键盘a11y、PORT容错、口试结束端点、README重写 ✅

### 第三轮（2026-08-02）— 按 OPTIMIZATION_PLAN.md 全量实施
- P0-1: 内联脚本抽离到 static/app.js（defer），修致命重复 `let allProblems`；node --check 通过 ✅
- P0-2: config.py 新增 PHYSICS_OS_API_KEY/API_BASE/MODEL env；密钥 env>内存>DB，绝不落库 ✅
- P0-3: 写请求 X-Requested-With 校验（403 拦截），GET 放行 ✅
- P1-1: KaTeX 下载到 static/vendor/（css/js/auto-render + 20 woff2），离线可渲染 ✅
- P1-2: /api/export 只读导出 + /api/import 参数化写入并自动备份 ✅
- P1-3: GET 失败重试1次 + 写操作 X-Request-Id 幂等键（服务端去重） ✅
- P1-4: 前端真分页（?q 搜索 + 排序 + 分页器） ✅
- P1-5/6/8/9: 导航 button、自定义确认框、焦点陷阱、label for/id ✅
- P1-7/10: 加载态(skeleton/思考中)、掌握度1-5与复习1-4 UI 标注（不改 schema） ✅
- P1-11: mastery_log 表(v2迁移) + 前端 SVG 趋势线 ✅
- P1-12: SQLite WAL + .gitignore 补 *.db-wal/-shm/-journal/-bak ✅
- P1-13: RotatingFileHandler + SecretRedactor 脱敏；config.py 顺带修 LOG 定义顺序 Bug ✅
- P2-1~9: 深色模式、口试思考指示、搜索防抖、Toast aria-live、hash 深链+数字键、列表排序、移动端间距、复习手动控制 ✅
