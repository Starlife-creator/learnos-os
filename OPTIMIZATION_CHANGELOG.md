# learnos-os 优化变更日志

> 本日志基于 CODE_REVIEW.md 审查结论落地。项目已自带 OPTIMIZATION_PLAN.md（功能缺口路线），
> 本批优化聚焦其未覆盖的工程侧：性能、健壮性、安全加固。参考 CODE_REVIEW.md 与变更对照。

## 改动清单

### 1. 并发 / 慢速请求防护（P1）
- app.py：新增 _BoundedThreadingHTTPServer，用 BoundedSemaphore 把并发处理线程限制到 32；
  超出连接留在内核 accept 队列等待，不再无限创建线程（慢速连接洪泛的资源耗尽防御）。
- handler.py：read_json 改为分块读取 + 30s 总时长预算(_BODY_READ_BUDGET)，
  防止慢速 body 无限占住线程，超时按 400 处理并断开。
- 修正认知：BaseHTTPRequestHandler.setup() 本已消费 self.timeout(120s) 做 socket 超时，
  真正缺口是无线程上限 + 无 body 读时长预算，均已补齐。

### 2. AI 重试退避 + 抖动（P1）
- ai.py：新增 _retry_sleep()，指数退避(1s 基数，30s 封顶) + 随机抖动，并尊重 Retry-After/额外下限。
- 约束：429 保持"配额/限流"语义不重试（被 test_handler / test_optimization_tierC /
  test_security_r6 三处测试钉死）；退避仅作用于 URLError/5xx 等可重试项。
- 重试间隔改模块级常数 _RETRY_BASE_DELAY / _RETRY_MAX_DELAY，便于测试下调提速。

### 3. 错误成因可见（P1）
- handler.py：_safe_error 新增 RuntimeError 分支——AI 层抛出的受控中文成因
  (空内容/仅推理/无法连接等)回显给前端，便于自助排障；仍抹路径 + 限长，敏感细节只进服务端日志。

### 4. SQL 标识符白名单守卫（P2 纵深防御）
- db.py：新增 safe_ident()，对表名/列名/索引名做 [A-Za-z_][A-Za-z0-9_]* 白名单断言，非法即抛错。
- 接入：db.py 两处迁移 DDL、handler.py 学科删除循环、learn.py 的 UPDATE materials SET 片段。
- 说明：这些标识符当前全部来自代码内硬编码白名单，此守卫防未来误引用户输入造成注入。

### 5. 日志已知密钥精确脱敏（P2）
- config.py：新增 register_secret() + _KNOWN_SECRETS，把当前密钥值本身纳入 SecretRedactor
  精确子串替换（对形状正则的兜底，覆盖无前缀/自定义格式密钥）。
- ai.py：set_runtime_key() 与 get_cached_settings() 解析出运行密钥后调用 register_secret()。

### 6. 媒体文件流式发送（P2）
- handler.py：_serve_media 改用 shutil.copyfileobj(..., 256KB) 分块直写 socket，
  不再 read_bytes() 全量入内存（大图/拍照附件并发时内存峰值下降）。

## 验证

- 语法：6 个修改文件 AST 全部通过；项目目录内实测 markers 齐全（2026-09-09 挂载恢复后核对）。
- 沙箱行为冒烟 7/7：safe_ident 防注入、register_secret 精确脱敏、_retry_sleep 退避与 Retry-After 下限、
  _BoundedThreadingHTTPServer 信号量、read_json 分块/超限/负长、_serve_media 流式。
- 功能级 9/9。
- **完整回归 + 基线对比（决定性）**：在含完整 data/ 种子数据的沙箱副本上跑 587 条测试，
  编辑后代码结果 = failures 2 + errors 8 + skipped 1；用编辑前原件覆盖重跑基线，
  失败清单与编辑后**逐条完全一致**，即优化引入的回归为 **0**。
- 剩余 10 项失败均为环境性，原代码同样失败，与本次改动无关：
  - `test_ci_and_readme_reference_single_entrypoint`：沙箱未拷 `.github/workflows/ci.yml`
  - `test_fsrs_bridge` ×3 + `test_train_parameters_insufficient_uses_constant`：缺第三方 `fsrs` 包（NameError: Card 未定义）
  - `test_ocr` ×2：缺 pypdf/Pillow 等 OCR 依赖（pdfminer 无法解析测试生成的 PDF）
  - `test_subject_exists_uses_normalize`：测试临时库未建 subjects 表（测试环境问题）
  - `test_02_problem_learning_loop` / `test_analytics_forgetting_curve`：与运行环境时序/数据相关的既有失败
- 建议在本机项目根目录运行 `python run_tests.py`（本机有完整 .github/、可选依赖与真实 DB），对照本基线。

## 交付 / 回滚
- 优化后的 6 个文件已在项目目录生效（ai.py、app.py、config.py、db.py、learn.py、handler.py）。
- 编辑前原始备份：/mnt/work/learnos_backup_opt/（如需回滚，用其覆盖对应文件）。
- 云端持久副本：artifacts/learnos_optimized/（含 CODE_REVIEW.md）。
