# 0008 AI 层按传输/提示词/降级/编排分层

状态：生效

## 决策

原 `ai.py` 单文件 1882 行，混装四类互不相干的关注点：HTTP 传输、结果缓存、设置与密钥、
学科提示词、离线降级启发式、以及调用它们的业务编排。按依赖方向拆成四层，**单向**依赖：

| 模块 | 职责 | 允许依赖 |
|---|---|---|
| `ai_client.py` | 结果缓存、设置与密钥、HTTP/流式调用、模型预设、SSRF 校验、重试 | 仅 config/db/keystore/validate |
| `ai_prompts.py` | 学科档案（`_subject_profile`）、阶段提示词、结构化 schema 常量 | 无（纯常量与纯函数） |
| `ai_fallback.py` | 断网/未配置时的本地提示与启发式产出（`fallback_hint`、`local_*`） | `ai_prompts` |
| `ai.py` | 对外入口：`explain_concept`/`extract_tags`/`generate_variants`/`generate_bank_question`/`review_bank_question`/`ai_score_item`/`ai_configured` | 上三层 |

`ai.py` 显式 re-export 三层旧名，并提供模块级 `__getattr__` 逐层回退，
因此 `from ai import X`、`ai.X`、`import ai` 全部保持兼容，调用方零改动。

## 原因

- 改一处提示词要读 1882 行文件；提示词与 HTTP 重试逻辑在同一屏里，评审成本高。
- 分层后依赖单向：`ai_prompts.py` 可单独 import 做纯函数测试，不触发网络与数据库。
- 降级层零网络依赖，可保证「断网仍可用」这一产品约束不被传输层改动破坏。

## 后果

- **可变模块级状态不 re-export**：`cache_hits`/`_result_mem`/`_runtime_key`/`_settings_cache`
  等若 `from ai_client import X`，拿到的是导入瞬间的值拷贝。它们全部交给 `__getattr__`
  实时回退读取，避免「改了 ai_client 的计数，读 ai 的计数还是旧的」这类静默错误。
  同理 `ai_configured()` 的兜底改为调用 `ai_client.runtime_key_present()`。
- **测试 patch 必须打在实现层**：`patch("ai.call_ai")` 仍有效（应用层按名字查找已被替换的
  全局名），但 patch 传输层内部（`_prepare_ai_request`/`_post_json`/`get_cached_settings`）
  必须改为 `patch.object(ai_client, ...)`，否则 patch 打在不被调用的副本上、静默失效。
  已按此更新 4 个测试文件。
- 新增 `TestFacadeReexports` 守卫：三层所有顶层名必须能经 `ai.` 取到，否则拆分漏名会
  在运行时才炸。
