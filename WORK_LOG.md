# 工作日志（WORK LOG）

> 每次 AI 迭代完成后的落地摘要，按日期追加。对应 `OPTIMIZATION_PLAN_V2.md` 各批次。

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