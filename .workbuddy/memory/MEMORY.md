# 项目长期记忆

## 项目约定
- **所有改动和影响严格限制在项目文件夹内**（`E:\tool\biancheng\AI project 1\physics-study-os\`），不修改项目外的任何文件。
- Python 运行时使用受管版本：`C:\Users\Lenovo\.workbuddy\binaries\python\versions\3.13.12\python.exe`
- 项目零第三方依赖原则，尽量只用 Python 标准库。

## 项目概况
- 本地优先的物理学习辅助工具（错题管理 + 三级AI提示 + 间隔复习 + AI口试）
- 后端：模块化结构 (app.py + config.py + db.py + ai.py + review.py + oral.py + handler.py)
- 前端：static/index.html 单页应用 (vanilla HTML/CSS/JS)
- 测试：tests/ 目录，61 个单元测试全部通过
- 复习算法：SM-2 (SuperMemo 2)，含 ease_factor 和 repetition 跟踪
- 构建：build.spec PyInstaller 脚本
- 数据库：SQLite，含版本化迁移机制（schema_version 表）
- 前端：KaTeX LaTeX 渲染 + 响应式布局 + 键盘可访问性

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
