# learnos-os 工程审查报告

> 审查对象：`learnos-os`（Python 标准库单体学习平台，零第三方运行时依赖）
> 审查维度：安全性、健壮性/错误处理、性能、架构/可维护性、测试。
> 说明：项目已自带 `OPTIMIZATION_PLAN.md`（B0–B6 多数已落地，聚焦 D1-D4/F/M/G/C/U/X/P2 等**功能**缺口与持久化、FSRS、掌握度、图谱溯源）。本报告**不重复**那份功能路线，只补充它未覆盖的**工程侧**优化点，并按优先级排序。

---

## 结论摘要

安全基本面扎实，属于同量级本地工具中较好的实现：密钥 AES-GCM 加密且不落库、CSRF 头 + 暴露态 Bearer、一次性 HMAC 导出挑战绑定真实 TCP 对端 IP（不信任可伪造的 `X-Forwarded-For`）、SQLite WAL + 生产级 PRAGMA、日志脱敏、审计留痕、静态服务受限目录、`_serve_media` 已堵路径穿越、`_SUBJECT_SCOPE_TABLES` 用硬编码白名单。

因此下面的优化**不是修大漏洞**，而是在这个已不错的基础上做**可靠性、可扩展性、健壮性**的加固。

---

## 一、优先修复（P1）

### 1. 请求/连接无超时 —— 慢速 body 可占住线程（暴露态是 DoS 向量）
`ThreadingHTTPServer` + 每请求一线程，未设置 `settimeout` / `socket.setdefaulttimeout`；`read_json` 按 `Content-Length` 读 body 也没有读超时。

- **风险**：恶意或半开的连接可以无限期占住一个 worker 线程。在 `is_exposed()`（HOST 非回环）时这是可被利用的资源耗尽向量；即便本机，一个卡住的上游也会拖住线程。
- **建议**：启动时 `socket.setdefaulttimeout(30)`；或在 handler 的 `setup()` 里 `self.connection.settimeout(30)`；`read_json` 增加读超时并对外返回 408。对流式 SSE 路径单独设置更长的读超时。

### 2. AI 调用重试无退避 / 无抖动 —— 放大上游故障负载
`for attempt in range(retries + 1)` 的循环内**没有**任何 `time.sleep`/退避。`URLError`（网络抖动）和 HTTP 429（限流）都会立即重试。

- **风险**：上游一旦抖动或限流，会以最大并发密集重打，反而恶化 429/错误率。
- **建议**：指数退避 + 抖动，例如 `backoff = min(2**attempt, 60) + random.uniform(0, 1)`，并对 429 读取 `Retry-After` 头；对 `URLError` 与 429 分级（429 少重试或按上游指示）。`ai.py` 已 `import time`，可直接用。

### 3. 错误被吞：AI `RuntimeError`（空内容/仅推理/模型不存在）落为通用 500
代码在 `call_ai` 里已为常见失败场景（空 content、reasoning-only、模型不存在、上下文超长）构造了**有用**的 `RuntimeError` 文案，但 `_safe_error` 的 else 分支把所有 `RuntimeError` 收敛成通用「服务器内部错误」。

- **风险**：用户只看到 500，无法区分「没生成内容」「只有推理」「模型不对」。既损失可诊断性，也让合理的内部提示到不了 UI。
- **建议**：为 `RuntimeError` 建立**受控的分类映射**（白名单短文案），在**不泄露 URL/远程内容**的前提下把成因提示返回给前端；敏感细节仍只进服务端日志。

---

## 二、建议改进（P2）

### 4. f-string 拼 SQL 标识符：当前受控，缺**断言守卫**防回归注入
存在 `DELETE FROM {tbl}`（delete_subject）、`learn.py` 的 `UPDATE ... SET`、`db.py` 迁移的 `ALTER TABLE ... ADD COLUMN {col}` 等 f-string 拼接标识符。当前安全是因为这些标识符都来自**硬编码白名单**（`_SUBJECT_SCOPE_TABLES`、固定的字段名、受控集合），**不是用户输入**。

- **风险**：这是「靠约定而非靠结构」的安全性。后续任何开发者若把一个用户可控的表名/列名传入同一代码路径，就会变成注入，且无任何防线。
- **建议**：加一个集中式 `_safe_ident(name)`：先断言 `re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)` 且 `name in ALLOWLIST`，否则抛异常；所有拼接前强制调用。并补一条单测覆盖「非白名单标识符被拒」。

### 5. 全局 `DB_LOCK` 串行化所有读（`rows()`/`row()` 也持锁）
`rows()`/`row()` 每次查询都持有同一把全局 `RLock`，写路径也持它。代码注释已注明「单用户场景可接受」。

- **风险**：多请求（例如一边在流式生成、一边浏览图谱/材料）时，读会与写互相阻塞，成为并发上限；若未来有 RAG/批量生成，长查询会拖住全局。
- **建议**：如无并发需求可保持现状（个人工具合理），至少**在文档中把它标注为已知容量上限**；若需扩展，可按「读共享 / 写独占」拆开，或对低频长查询用独立连接。此点属可选项，不建议过度设计。

### 6. `_serve_media` 用 `read_bytes()` 全量读入内存
媒体文件经 `read_bytes()` 一次性载入内存再回写。对拍照/大图附件，单请求会瞬时占用等于文件大小的内存。

- **风险**：并发访问多个大附件时内存峰值叠加；`ctype` 白名单外的文件回退 `application/octet-stream`。
- **建议**：改为按 chunk 流式返回并设置 `Content-Length`（`rfile`/`sendfile` 思路），至少对超阈值文件封顶或流式；可保留扩展名白名单，非白名单用明确且安全的内容类型。

### 7. 日志脱敏仅靠「形状」正则，未按「已知 key 值」子串脱敏
脱敏器用 `SecretRedactor` 匹配 `sk-`、`Bearer`、`key=`、`token=` 等**形状**。

- **风险**：若某个密钥不以这些常见前缀出现（例如自定义格式、无前缀明文），在错误串/日志里可能**漏脱敏**而泄漏。
- **建议**：在脱敏器里额外加入**当前已配置的密钥值本身**的精确子串替换（把 `api_key`/`EXPORT_TOKEN` 的已知值整体替换为 `***`），作为对形状匹配的兜底；仅在密钥已配置时启用。

---

## 三、可选优化（P3）

### 8. `probe_ollama(timeout=1.5)` —— 探活用短超时是合理的，保持即可
无需改动，仅确认该短超时是**预期的**（探测类请求应快），不是错误。

### 9. 日志敏感字段的「路径脱敏」策略
`_safe_error` 对 `JSONDecodeError`/`ValueError` 返回含**路径被截断**的 400 文案，`RuntimeError` 走通用 500。建议统一：面向用户的错误永远只给**受控分类文案**，所有原始异常进服务端日志（含 stack）。

### 10. 反向代理场景
`_client_ip` 取 `client_address[0]`（真实 TCP 对端）——这一点对**本地直连**是正确的，也不可被伪造。但若部署在**反向代理后**，所有请求会呈现为代理 IP，会破坏「一次性挑战绑定 IP」与「按 IP 限流/审计」。
- **建议**：无需改，但在 README 或注释中**明确说明**「本工具按本地直连设计，代理后 IP 绑定/限流会退化为代理单点」，提示用户如需公网代理应自行加反向代理层的限流/鉴权。

### 11. 并发线程无上限
`ThreadingHTTPServer` 按请求创建线程且无上限/队列限制。本机单用户可接受；仅当要防御公网洪泛时才需要引入 `Semaphore`（类似并发信号量）或请求队列上限。
- **建议**：保留为已知边界即可，除非转向公网。

---

## 四、测试与可维护性（现状良好）

- `tests/` 下约 37 个测试文件，`run_tests.py` 为权威入口并带 shim，报告约 586 通过，覆盖良好。
- 零第三方运行时依赖，符合自托管/离线哲学；`cryptography` 为可选加固（`keystore` 已优雅降级为纯内存）。
- 代码结构清晰（`app`/`handler`/`db`/`ai`/`graph`/`learn`/`material`/`keystore`/`auth`/`ratelimit` 模块化）。

---

## 五、本次交付的「最小改动清单」（建议落地顺序）

| 优先级 | 改动 | 文件 | 说明 |
|---|---|---|---|
| P1 | 连接/请求超时 | app.py / handler(`setup`) | 慢速 body 防线程占用法 |
| P1 | AI 重试退避+抖动、尊重 Retry-After | ai.py | 放大上游故障 |
| P1 | AI RuntimeError 分类映射 | ai.py / handler | 让成因提示可见、不泄敏感 |
| P2 | `_safe_ident()` 白名单断言守卫 | db.py / learn.py / handler | 防 SQL 拼标识符回归注入 |
| P2 | 日志按「已知 key 值」精确脱敏 | 脱敏器 | 兜底形状匹配漏网 |
| P2 | 媒体流式/封顶 | handler(_serve_media) | 大图内存峰值 |
| P3 | README 标注直接连接设计、并发上限、代理注意点 | README | 降低误用 |

---

*本报告基于对源码的静态走查（config/keystore/auth/ratelimit/db/handler/ai/learn/app 及 OPTIMIZATION_PLAN.md），未做运行时压测；P1/P2 均给出可执行的落点。*
