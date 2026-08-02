# Physics Study OS — 个人物理学习 OS

> 本地优先、零第三方依赖的物理学习辅助工具。
> 记录尝试 → 分级提示 → 主动回忆 → 间隔复习 → AI 口试。

## 功能

- **错题管理**：记录题目、自己的尝试、错误类型、掌握度
- **三级 AI 提示**：不给答案，逐步引导——概念检查 → 方向指引 → 解题框架
- **SM-2 间隔复习**：基于 SuperMemo 2 算法的动态复习调度
- **AI 口试**：围绕一个知识点进行五轮追问，检验真正的理解
- **仪表盘**：掌握度统计、薄弱知识点、最近记录
- **LaTeX 渲染**：KaTeX 支持 `$...$` 和 `$$...$$` 公式
- **响应式布局**：桌面/平板/手机自适应

## 快速开始

```bash
# 克隆项目
cd physics-study-os

# 启动（自动打开浏览器）
python app.py

# 或指定端口
set PHYSICS_OS_PORT=9000 && python app.py
```

打开 `http://127.0.0.1:8765` 即可使用。

## 配置

在「设置」页面配置 AI 接口（兼容 OpenAI API 格式）：

| 配置项 | 环境变量 | 默认值 |
|--------|---------|--------|
| API 地址 | `PHYSICS_OS_API_BASE` | `https://api.openai.com/v1` |
| API Key | `PHYSICS_OS_API_KEY` | 空（环境变量 / 内存优先，绝不写入数据库） |
| 模型名称 | `PHYSICS_OS_MODEL` | 空 |
| Temperature | - | `0.3` |
| 数据库路径 | `PHYSICS_OS_DB` | 项目目录下 `physics_study.db` |
| 监听地址 | `PHYSICS_OS_HOST` | `127.0.0.1` |
| 监听端口 | `PHYSICS_OS_PORT` | `8765` |

> **密钥安全**：API Key 仅来自环境变量或本次运行内存（UI 录入后重启失效），**永远不会以明文写入数据库文件**。设置页会显示当前密钥来源（environment / runtime / local）。

### 数据备份与迁移

- **导出**：概览页「导出数据」生成 `physics_study_YYYY-MM-DD.json`（题目 + 提示 + 复习记录）。
- **导入**：概览页「导入数据」会**先自动备份**当前数据库，再以参数化写入导入，不会造成注入或数据损坏。导入前会弹出确认框。

### 安全

- 所有写请求（POST/PUT/DELETE）需携带 `X-Requested-With` 头，缺失即 403——防范同机恶意网页对 localhost 的跨站调用。
- SQLite 启用 WAL 模式提升并发读写；日志写入 `physics_study.log`（滚动 1MB × 3，自动脱敏密钥）。

## 项目结构

```
physics-study-os/
  app.py           # 主入口：启动服务器
  config.py        # 全局配置、路径常量、Schema
  db.py            # 数据访问层：SQLite 连接、查询、迁移
  ai.py            # AI 调用层：OpenAI 兼容接口、提示词、降级
  review.py        # SM-2 间隔复习算法
  oral.py          # AI 口试模块
  handler.py       # HTTP 路由处理器
  static/
    index.html     # 前端单页应用
  tests/
    test_review.py # SM-2 算法测试
    test_ai.py     # AI 函数测试
    test_db.py     # 数据库层测试
    test_handler.py# HTTP 端点测试
    test_app.py    # 端到端集成测试
  build.spec       # PyInstaller 打包脚本
  start.bat        # Windows 快速启动
```

## 测试

```bash
python -m unittest discover -s tests -v
```

覆盖范围：SM-2 算法、AI 提示词构造与降级、数据库 CRUD 与密钥遮蔽、HTTP 端点全路径、端到端学习循环。

## 构建

```bash
# 安装 PyInstaller
pip install pyinstaller

# 打包为单文件 exe
pyinstaller build.spec
```

产物在 `dist/PhysicsStudyOS.exe`。

## 技术栈

- **后端**：Python 标准库（`http.server` + `sqlite3` + `urllib`），零第三方依赖
- **前端**：Vanilla HTML/CSS/JS，`static/app.js` + 本地 `static/vendor/` KaTeX（离线可用）
- **算法**：SM-2 (SuperMemo 2) 间隔复习
- **打包**：PyInstaller

## 学习方法论

1. **记录尝试**：先自己写，哪怕写错——错在哪里比正确答案更有价值
2. **分级提示**：从概念检查到解题框架，三级递进，AI 不代写作业
3. **主动回忆**：SM-2 算法在最佳遗忘点安排复习，强制提取记忆
4. **口试追问**：五轮深度提问，检验你是否能用自己的话解释概念

## 许可

个人学习使用。
