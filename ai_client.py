"""AI 传输层：结果缓存、设置与密钥、HTTP 调用、模型预设。

由原 ai.py 拆出（ADR 0008）。本层不依赖提示词与降级层，可单独测试。
"""
from __future__ import annotations

import ipaddress
import copy
import random
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from config import LOG, API_KEY_ENV, API_BASE_ENV, MODEL_ENV
from db import settings_dict
from keystore import load_key, key_file_exists
from validate import validate_object, SchemaError


# ── 应用层结果缓存（R12）──────────────────────────────────────────
# 让"结果稳定可复用"的 AI 调用（审题/标签提取等）命中缓存省 token。
# 双层：内存 LRU（快）+ SQLite（跨重启）。TTL 与容量都有上限防膨胀。
_RESULT_CACHE_TTL = 30 * 24 * 3600     # 30 天（区别于下方 settings 缓存 _CACHE_TTL=30s）
_CACHE_MAX_MEM = 200            # 内存最多缓存 200 条
_result_mem: dict[str, tuple[float, dict[str, Any]]] = {}

# 进程内缓存命中计数（P1b 可观测：验证缓存收益、定位失效；不写库、无迁移）
_cache_metrics_lock = threading.Lock()
cache_hits = 0
cache_misses = 0


def _inc_cache(hit: bool) -> None:
    global cache_hits, cache_misses
    with _cache_metrics_lock:
        if hit:
            cache_hits += 1
        else:
            cache_misses += 1


def get_cache_metrics() -> tuple[int, int, float]:
    """返回 (hits, misses, ratio)。重启即失，仅作运营观测。"""
    with _cache_metrics_lock:
        h, m = cache_hits, cache_misses
    ratio = (h / (h + m)) if (h + m) else 0.0
    return h, m, round(ratio, 4)


def _ensure_cache_table() -> None:
    try:
        from db import DB_LOCK, db, now
        with DB_LOCK, db() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ai_result_cache ("
                " cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL)")
    except Exception as exc:
        LOG.warning("创建结果缓存表失败: %s", exc)


def cache_get(key: str) -> dict[str, Any] | None:
    """读缓存：先内存后 DB；命中返回 dict，过期/缺失返回 None。"""
    import hashlib
    k = hashlib.sha256(key.encode("utf-8")).hexdigest()
    mem = _result_mem.get(k)
    if mem:
        ts, val = mem
        if time.time() - ts < _RESULT_CACHE_TTL:
            _inc_cache(True)
            return copy.deepcopy(val)  # 深拷贝：调用方原地写标记字段不得污染缓存条目
        _result_mem.pop(k, None)
    try:
        from db import DB_LOCK, db
        from validate import SchemaError as _SE
        with DB_LOCK, db() as conn:
            row = conn.execute(
                "SELECT payload, created_at FROM ai_result_cache WHERE cache_key = ?", (k,)).fetchone()
        if row:
            # TTL 对 DB 层同样生效：created_at 为 db.now() 的 ISO 串，过期即删并按未命中处理
            from datetime import datetime as _dt
            try:
                age = time.time() - _dt.fromisoformat(row["created_at"]).timestamp()
            except (ValueError, TypeError, OverflowError):
                age = _RESULT_CACHE_TTL  # 时间戳损坏视同过期
            if age >= _RESULT_CACHE_TTL:
                with DB_LOCK, db() as conn:
                    conn.execute("DELETE FROM ai_result_cache WHERE cache_key = ?", (k,))
                row = None
        if row:
            val = json.loads(row["payload"])
            _result_mem[k] = (time.time(), val)
            _inc_cache(True)
            return copy.deepcopy(val)
    except (Exception, _SE) as exc:
        LOG.debug("读结果缓存失败: %s", exc)
    _inc_cache(False)
    return None


def cache_set(key: str, value: dict[str, Any]) -> None:
    """写缓存（DB + 内存）。内存容量超限时清最旧。"""
    import hashlib
    try:
        k = hashlib.sha256(key.encode("utf-8")).hexdigest()
        from db import DB_LOCK, db, now
        payload = json.dumps(value, ensure_ascii=False)
        with DB_LOCK, db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ai_result_cache(cache_key, payload, created_at) VALUES (?, ?, ?)",
                (k, payload, now()))
        _result_mem[k] = (time.time(), value)
        if len(_result_mem) > _CACHE_MAX_MEM:
            oldest = sorted(_result_mem.items(), key=lambda kv: kv[1][0])[: len(_result_mem) - _CACHE_MAX_MEM]
            for o_k, _ in oldest:
                _result_mem.pop(o_k, None)
        return None
    except Exception as exc:
        LOG.warning("写结果缓存失败: %s", exc)
        return None


_ensure_cache_table()

_settings_cache: dict[str, str] | None = None
_settings_cache_time: float = 0
_CACHE_TTL = 30  # 秒

# 内存密钥：通过 UI 录入时仅存于内存，绝不写入数据库（R4）。
_runtime_key: str | None = None
# 可选 keys.enc 主密钥口令（用户提供，经环境变量或 UI 传递，不落库不落盘）。
_master_password: str | None = None


def invalidate_settings_cache() -> None:
    """设置更新后调用，清除缓存。"""
    global _settings_cache, _settings_cache_time
    _settings_cache = None
    _settings_cache_time = 0


def set_runtime_key(key: str | None) -> None:
    """设置内存密钥（会话级，重启后失效）。"""
    global _runtime_key
    _runtime_key = (key or "").strip() or None
    if _runtime_key:
        from config import register_secret
        register_secret(_runtime_key)  # P2：已知密钥值纳入日志精确脱敏
    invalidate_settings_cache()


def set_master_password(password: str | None) -> None:
    """设置 keys.enc 主密钥口令（会话级）。"""
    global _master_password
    _master_password = (password or "").strip() or None
    invalidate_settings_cache()


def unlock_keyfile(password: str | None) -> bool:
    """用主口令解锁 keys.enc（验证口令并置会话口令）。成功返回 True。

    未验证口令前不设置会话口令，避免错误口令被缓存。
    """
    password = (password or "").strip()
    if not password or not key_file_exists():
        return False
    if load_key(password):
        set_master_password(password)
        return True
    return False


def reset_session_key() -> None:
    """清空会话级密钥（模拟重启：内存密钥/主口令失效，keys.enc 文件保留）。"""
    global _runtime_key, _master_password
    _runtime_key = None
    _master_password = None
    invalidate_settings_cache()


def clear_session_key() -> None:
    """清除内存密钥与 keys.enc（用户主动「清除全部密钥」）。"""
    reset_session_key()
    from keystore import clear_key
    clear_key()


def get_cached_settings() -> dict[str, str]:
    """带 TTL 的设置缓存，按 环境变量 > keys.enc > 内存密钥 > DB(非敏感) 合并。"""
    global _settings_cache, _settings_cache_time
    if _settings_cache is not None and time.time() - _settings_cache_time <= _CACHE_TTL:
        return _settings_cache
    s = dict(settings_dict(include_secret=True))
    if API_KEY_ENV:
        s["api_key"] = API_KEY_ENV
        s["key_source"] = "environment"
    elif key_file_exists() and _master_password:
        stored = load_key(_master_password)
        if stored:
            s["api_key"] = stored
            s["key_source"] = "keyfile"
        else:
            s["api_key"] = _runtime_key or ""
            s["key_source"] = "runtime" if _runtime_key else "none"
    elif _runtime_key:
        s["api_key"] = _runtime_key
        s["key_source"] = "runtime"
    else:
        s["api_key"] = ""
        s["key_source"] = "none"
    if API_BASE_ENV:
        s["api_base"] = API_BASE_ENV
    if MODEL_ENV:
        s["model"] = MODEL_ENV
    _ak = s.get("api_key", "")
    if _ak:
        from config import register_secret
        register_secret(_ak)  # P2：无论密钥来自 env/keys.enc/内存，均纳入日志精确脱敏
    _settings_cache = s
    _settings_cache_time = time.time()
    return s


def display_settings() -> dict[str, str]:
    """供设置页展示：脱敏后的有效配置与密钥来源。"""
    eff = get_cached_settings()
    has_key = bool(eff.get("api_key"))
    return {
        "api_base": eff.get("api_base", ""),
        "model": eff.get("model", ""),
        "fast_model": eff.get("fast_model", ""),
        "heavy_model": eff.get("heavy_model", ""),
        "vision_model": eff.get("vision_model", ""),
        "embedding_model": eff.get("embedding_model", ""),
        "temperature": eff.get("temperature") or "0.3",
        "default_subject": eff.get("default_subject", "physics"),
        "hint_cache_enabled": eff.get("hint_cache_enabled", "1") != "0",
        "daily_review_cap": int(eff.get("daily_review_cap", "0") or 0),
        "ai_context_tokens": int(eff.get("ai_context_tokens", "32000") or 32000),
        "allow_local_ai": eff.get("allow_local_ai", "1") != "0",
        "disable_thinking": eff.get("disable_thinking", "1") != "0",
        "max_output_tokens": int(eff.get("max_output_tokens", "4096") or 4096),
        # M1/M6 高级开关（默认关，端点不支持时自动 400 剥离降级）
        "prompt_cache_control": eff.get("prompt_cache_control", "0") == "1",
        "json_response_format": eff.get("json_response_format", "0") == "1",
        "has_api_key": has_key,
        "key_source": eff.get("key_source", "none"),
        # 存在 keys.enc 但当前未解锁（key_source 为 none/runtime）时提示可解锁
        "key_file_locked": bool(key_file_exists()) and not has_key,
    }


def estimate_tokens(text: str) -> int:
    """U2 上下文 token 估算（零依赖启发式，与 material.batch_chars 同口径）。

    中文 ≈ 1.1 字符/token，ASCII ≈ 4 字符/token（混合文本按字符类分别累计）。
    仅用于「引用面板」的预算展示与注入前的粗裁剪，不追求分词器级精确。
    """
    s = str(text or "")
    cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
    other = len(s) - cjk
    return int(cjk / 1.1 + other / 4) + 1


def api_endpoint(base: str) -> str:
    """拼接 chat/completions 端点。仅允许 http/https 协议（防 file:/data: 等任意 URI 读取）。

    主机不做私网/环回限制：本地优先设计，用户自配本地 Ollama（127.0.0.1:11434）
    是核心特性；服务仅监听 127.0.0.1 单用户使用，写请求另有 CSRF 头闸门。
    """
    base = base.strip().rstrip("/")
    if not base.lower().startswith(("http://", "https://")):
        raise ValueError("API 地址必须以 http:// 或 https:// 开头")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def is_local_endpoint(base: str) -> bool:
    """本地模型（Ollama 等）端点允许空密钥（C3）。"""
    base = (base or "").strip().lower()
    return base.startswith("http://localhost") or base.startswith("http://127.0.0.1") or base.startswith("http://[::1]")


# 模型探测结果缓存：本地 Ollama 可用性不会秒级变化，缓存避免每次页面加载都付探测延迟。
_PROBE_CACHE_TTL = 30.0
_probe_cache: dict[str, Any] = {"ts": 0.0, "val": None}


def probe_ollama(timeout: float = 1.5) -> dict[str, Any] | None:
    """C3 探测本地 Ollama 服务（用户自装，仅探测不安装）。失败返回 None。

    优化：① 直连 localhost 并禁用代理（否则被 HTTP_PROXY 劫持后连接挂满超时）；
    ② 结果缓存 30s，避免设置页每次加载都付 1.5~3s 探测延迟。
    """
    now = time.monotonic()
    if now - _probe_cache["ts"] < _PROBE_CACHE_TTL:
        return _probe_cache["val"]
    try:
        request = urllib.request.Request(
            "http://localhost:11434/api/tags", method="GET",
            headers={"User-Agent": "LearnOS/0.5"},
        )
        # 直连 localhost，绕过代理（localhost 探测不应走代理，否则挂起）
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        models = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
        result = {"available": True, "models": models}
    except Exception as exc:
        LOG.debug("Ollama 探测失败: %s", exc)
        result = None
    _probe_cache["ts"] = now
    _probe_cache["val"] = result
    return result


def _resolve_model(config: dict[str, str], tier: str | None) -> str:
    """C2 双档：fast/heavy 覆盖默认 model；未配置则回退默认。"""
    model = config.get("model", "").strip()
    if tier in ("fast", "heavy"):
        tier_model = config.get(f"{tier}_model", "").strip()
        if tier_model:
            return tier_model
    return model


def _safe_temperature(config: dict[str, str]) -> float:
    """温度解析兜底：空串/非法值回退 0.3，并夹在 0-2（防 float('') 崩溃）。"""
    try:
        value = float(config.get("temperature", "0.3"))
    except (TypeError, ValueError):
        return 0.3
    if value != value:  # NaN：比较语义会让 min/max 静默返回边界值，先归掉
        return 0.3
    return max(0.0, min(2.0, value))


def _clamp_temperature(value: Any) -> float:
    """显式传入的 temperature 兜底：非法/NaN 回落 0.3（复用 _safe_temperature 的容错语义）。"""
    return _safe_temperature({"temperature": value})


# ── 模型预设库（全网调研 2026-08，提供商×模型名双重映射，llm-rosetta 推理行为表校准）──
# 各模型族关闭思考方式差异巨大，按"提供商默认端点 + 模型名"双重匹配注入参数。
# 要点：DeepSeek/Volcengine 拒绝 reasoning_effort=none（400），须用 thinking.type=disabled；
# OpenRouter 聚合层省略 thinking 对象即关闭（勿下推 provider 参数）；MiniMax/豆包/混元/GLM 均认 thinking.type=disabled。
_MODEL_PRESETS: list[dict[str, Any]] = [
    # ── OpenAI 推理系列：reasoning_effort=none（o1/o3/GPT-5 官方支持；经 OpenRouter 时不注入）──
    {"match": lambda m, b: any(k in m.lower() for k in ("o1", "o3", "gpt-5"))
                           and "openrouter" not in b.lower(),
     "body": {"reasoning_effort": "none"}},
    # ── 阿里云百炼（maas.aliyuncs.com）：DeepSeek/Qwen/Kimi 均 enable_thinking（非流式必须 false）──
    {"match": lambda m, b: "maas.aliyuncs.com" in b.lower()
                           and any(k in m.lower() for k in ("deepseek", "qwen", "kimi")),
     "body": {"enable_thinking": False}},
    # ── DeepSeek 官方（api.deepseek.com）：deepseek-chat 即非思考；若配 deepseek-reasoner，
    #    thinking.type=disabled 显式关思考（DeepSeek 拒绝 reasoning_effort=none）──
    {"match": lambda m, b: "deepseek" in m.lower() and "deepseek.com" in b.lower(),
     "body": {"thinking": {"type": "disabled"}}},
    # ── Kimi 官方（moonshot.ai/cn）：k2.5/k2.6 用 thinking.type=disabled（官方 thinking 参数）──
    {"match": lambda m, b: "kimi" in m.lower() and "moonshot" in b.lower(),
     "body": {"thinking": {"type": "disabled"}}},
    # ── GLM 智谱（open.bigmodel.cn / bigmodel.cn / z.ai）：GLM-4.5+ 用 thinking.type=disabled；
    #    GLM-5.2+ 也可 reasoning_effort=none（官方：none 模型放弃思考）──
    {"match": lambda m, b: ("glm" in m.lower() or "chatglm" in m.lower())
                           and any(k in b.lower() for k in ("bigmodel", "z.ai", "zhipu")),
     "body": {"thinking": {"type": "disabled"}}},
    # ── 豆包/火山方舟（volces.com 或 doubao）：thinking.type=disabled（Volcengine 显式禁用）──
    {"match": lambda m, b: ("doubao" in m.lower() or "seed" in m.lower())
                           and "volces.com" in b.lower(),
     "body": {"thinking": {"type": "disabled"}}},
    # ── 腾讯混元（hunyuan）：thinking.type=disabled──
    {"match": lambda m, b: "hunyuan" in m.lower() or "hunyuan" in b.lower(),
     "body": {"thinking": {"type": "disabled"}}},
    # ── MiniMax（minimaxi / minimax 端点）：M2+/Claude 兼容均支持 thinking.type=disabled（llm-rosetta）──
    {"match": lambda m, b: "minimax" in (m + " " + b).lower(),
     "body": {"thinking": {"type": "disabled"}}},
    # ── 阶跃星辰 Step（api.stepfun.com）：reasoning_effort 三档，最低档近似关思考──
    {"match": lambda m, b: "stepfun" in b.lower() or m.lower().startswith("step-"),
     "body": {"reasoning_effort": "low"}},
    # ── Gemini（Google 官方 OpenAI 兼容层）：reasoning_effort=none（Gemini 3.x thinking_level 映射）──
    {"match": lambda m, b: "gemini" in m.lower()
                           and "generativelanguage.googleapis.com" in b.lower(),
     "body": {"reasoning_effort": "none"}},
    # ── Grok（xAI）：OpenAI 兼容层认 reasoning_effort=none（官方兼容）──
    {"match": lambda m, b: "grok" in m.lower(),
     "body": {"reasoning_effort": "none"}},
    # ── Qwen 自托管/vLLM：chat_template_kwargs.enable_thinking=false──
    {"match": lambda m, b: "qwen" in m.lower() and "maas.aliyuncs.com" not in b.lower(),
     "body": {"chat_template_kwargs": {"enable_thinking": False}}},
    # ── OpenRouter 聚合层：省略 thinking 对象即关闭（网关自映射），勿下推 provider 参数──
    # ── 兜底：百川（baichuan-ai.com）/讯飞星火（xf-yun.com）/Claude/Llama/Ollama：不注入 ──
]


def _model_preset_body(model: str, base: str) -> dict[str, Any]:
    """按模型名/端点返回应注入的 body 参数（关闭思考）。未命中返回 {}。"""
    for preset in _MODEL_PRESETS:
        try:
            if preset["match"](model, base):
                return dict(preset["body"])
        except Exception:
            continue
    return {}


# 提示词版本（M7 工程化基线）：任何 system/角色提示词语义变更时递增
# （YYYY.MM），配合 /api/metrics 的 prompt_version 与 tests/test_prompt_upgrade.py
# 的措辞不变量测试，实现"改提示词必须跑回归"的最小闭环。
PROMPT_VERSION = "2026.08"


def _with_cache_breakpoint(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给首条 system 消息打 Anthropic 风格缓存断点（ephemeral）。

    仅在用户显式开启 prompt_cache_control 时使用：DeepSeek/OpenAI 对稳定前缀
    自动缓存无需此参数；部分严格端点会拒绝未知字段（由 call_ai 的 400 剥离
    重试兜底）。返回新列表，不修改入参。
    """
    out = [dict(m) for m in messages]
    for m in out:
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            m["content"] = [{"type": "text", "text": m["content"],
                             "cache_control": {"type": "ephemeral"}}]
            break
    return out


def _prepare_ai_request(
    messages: list[dict[str, str]],
    max_tokens: int,
    tier: str | None,
    route: str,
    stream: bool,
    model_override: str | None = None,
    skip_preset: bool = False,
    temperature: float | None = None,
    json_mode: bool = False,
) -> tuple[str, str, dict[str, str], dict[str, str], float]:
    """call_ai / call_ai_stream 共用的配置校验与请求构造。

    返回 (model, url, payload, headers, start)；未配置时记录 telemetry 并抛 ValueError。
    skip_preset=True 时不注入模型预设参数、缓存断点与 response_format
    （用于 400 未知参数后的降级重试）。
    json_mode=True 表示该调用点产出 JSON（M6）；是否真正下发 response_format
    还需用户开启 json_response_format——双闸门确保默认零行为变更。
    """
    from telemetry import record
    start = time.monotonic()
    config = get_cached_settings()
    api_key = config.get("api_key", "").strip()
    model = _resolve_model(config, tier)
    if model_override:
        model = model_override
    base = config.get("api_base", "").strip()
    if not model or not base or (not api_key and not is_local_endpoint(base)):
        record(route=route, model=model, ok=False, error_kind="not_configured", start=start)
        raise ValueError('请先在「AI 设置」中填写 API 地址、密钥和模型。')

    # 单次输出 token 上限：min(调用方请求, 用户设置的 max_output_tokens)
    # ——不同型号输出上限差异大，统一夹取防越界；也避免用户设小导致截断后盲目加码。
    cap = 0
    try:
        cap = int(config.get("max_output_tokens", "4096") or 4096)
    except (TypeError, ValueError):
        cap = 4096
    eff_max_tokens = max(256, min(max_tokens, cap))

    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": _safe_temperature(config) if temperature is None
                          else _clamp_temperature(temperature),
        "max_tokens": eff_max_tokens,
        "stream": stream,
    }
    # 模型预设库：按模型名/端点自动注入"关闭思考"参数。
    # 非流式请求必须关闭思考（reasoner 思考仅支持流式，否则报错/仅返回推理内容→JSON 解析失败）；
    # 流式 + 用户显式开启思考（disable_thinking=0）时保留，供深度推理场景。
    # skip_preset=True 时跳过（400 未知参数降级重试用）。
    if not skip_preset:
        preset = _model_preset_body(model, base)
        if preset:
            if not stream or config.get("disable_thinking", "1") != "0":
                for k, v in preset.items():
                    body.setdefault(k, v)
    # 提示词缓存断点（可选开关，默认关）：仅 Anthropic 原生/兼容代理需要显式
    # cache_control；DeepSeek/OpenAI 系自动缓存稳定前缀，无需开启。
    if not skip_preset and config.get("prompt_cache_control", "0") == "1":
        body["messages"] = _with_cache_breakpoint(body["messages"])
    # M6 结构化输出（可选开关，默认关）：把「只返回 JSON」从提示词约束升级为协议约束。
    # 仅对声明 json_mode 的调用点生效；不支持的端点会 400，由剥离重试降级。
    if not skip_preset and json_mode and config.get("json_response_format", "0") == "1":
        body["response_format"] = {"type": "json_object"}
    if stream:
        # 请求最后一块附带 usage（DeepSeek/OpenAI 支持；本地实现不识别则忽略）
        body["stream_options"] = {"include_usage": True}
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "LearnOS/0.5",
    }
    if stream:
        headers["Accept"] = "text/event-stream"
    return model, api_endpoint(base), payload, headers, start


def _target_is_private(url: str) -> bool:
    """解析 URL 主机：私网/环回/链路本地返回 True（解析失败按 False，交给连接层报错）。"""
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
        addr = ipaddress.ip_address(socket.gethostbyname(host))
        return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
    except (ValueError, socket.gaierror, OSError):
        return False


def _check_ai_target(url: str) -> None:
    """AI 出站目标边界校验（SSRF 防护）。

    - 仅允许 http/https 协议；
    - 私网/环回目标需显式同意：设置 allow_local_ai（默认开——本地 Ollama 是本应用
      核心特性；本服务仅绑定 127.0.0.1 单用户本机使用，写接口另有 CSRF 闸门）；
    - 不跟随重定向（防校验后被 3xx 跳到内网目标）。
    """
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"AI 端点协议必须是 http/https（当前: {scheme or '缺失'}）")
    if _target_is_private(url) and get_cached_settings().get("allow_local_ai", "1") != "1":
        raise ValueError("该端点解析到本地/内网地址。如需使用本地模型（Ollama 等），"
                         "请在设置中开启「允许本地/内网 AI 端点」。")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 禁止跟随：已校验的目标不可被 3xx 改写


_OPENER = urllib.request.build_opener(_NoRedirect)


def _cached_tokens(usage: dict[str, Any]) -> int:
    """输入侧 prompt 缓存命中 token：DeepSeek / OpenAI 兼容字段，缺失为 0。"""
    try:
        hit = usage.get("prompt_cache_hit_tokens")  # DeepSeek
        if hit is None:
            hit = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")  # OpenAI 系
        return int(hit or 0)
    except (TypeError, ValueError):
        return 0


# P1 优化：重试退避参数。base 按 2^attempt 指数增长并封顶；抖动避免并发同步重打上游。
# 模块级常数便于测试下调提速（retries 默认 1，本地故障转移场景最多多等 ~1.5s）。
_RETRY_BASE_DELAY = 1.0
_RETRY_MAX_DELAY = 30.0


def _retry_sleep(attempt: int, extra: float = 0.0) -> float:
    """指数退避 + 抖动，返回实际休眠秒数（extra 为下限，如 Retry-After）。"""
    delay = min(_RETRY_MAX_DELAY, _RETRY_BASE_DELAY * (2 ** attempt)) + random.uniform(0.0, 0.5)
    delay = max(delay, float(extra or 0.0))
    time.sleep(delay)
    return delay


def _post_json(url: str, payload: bytes, headers: dict[str, str], timeout: int = 45) -> dict[str, Any]:
    """发送 POST 并解析 JSON（入口过 _check_ai_target，经 _OPENER 禁重定向）。"""
    _check_ai_target(url)
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with _OPENER.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def call_ai(
    messages: list[dict[str, str]],
    max_tokens: int = 700,
    retries: int = 1,
    tier: str | None = None,
    model_override: str | None = None,
    route: str = "",
    temperature: float | None = None,
    json_mode: bool = False,
) -> str:
    """调 OpenAI 兼容接口。temperature 覆盖设置；None=用全局配置。

    json_mode=True 声明本调用点产出 JSON（M6）：用户开启 json_response_format 时
    下发 response_format=json_object；端点不支持则 400 后自动剥离降级。
    """
    from telemetry import record
    model, url, payload, headers, start = _prepare_ai_request(
        messages, max_tokens, tier, route, stream=False, model_override=model_override,
        temperature=temperature, json_mode=json_mode,
    )

    # 中转站式降级：预设注入的 thinking 参数若被端点 400 拒绝（未知参数），
    # 自动去除该参数重建请求重试一次（借鉴 LiteLLM/OpenRouter 的参数兼容策略）。
    _dropped_preset = False

    def _retry_without_preset() -> tuple[str, bytes | None]:
        nonlocal _dropped_preset
        try:
            m2, u2, p2, h2, _ = _prepare_ai_request(
                messages, max_tokens, tier, route, stream=False,
                model_override=model_override, skip_preset=True)
            _dropped_preset = True
            LOG.warning("AI 请求 400：去掉可选参数（thinking 预设/缓存断点/response_format）后重试。")
            return m2, p2
        except Exception:
            return "", None

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            result = _post_json(url, payload, headers, timeout=45)
            usage = result.get("usage") or {}
            tokens = int(usage.get("total_tokens") or 0)
            record(route=route, model=model, ok=True, tokens=tokens, start=start,
                   cached=_cached_tokens(usage))
            message = result["choices"][0]["message"]
            content = str(message.get("content") or "").strip()
            if not content:
                has_reasoning = bool(str(message.get("reasoning_content") or "").strip())
                if has_reasoning:
                    # reasoner 类模型本轮只输出了推理草稿、未给出最终答案（常见于
                    # 复杂任务 + 小 max_tokens 挤占）。把它当答案返回会让下游
                    # JSON 解析失败（char 0），故明确报错提示换模型/提额度。
                    raise RuntimeError(
                        "AI 仅返回了推理内容、未生成最终答案（reasoning_content 非空而 "
                        "content 为空）。建议：使用非推理模型，或在配置中提高 max_tokens。")
                raise RuntimeError(
                    "AI 返回了空内容（HTTP 200 但无文本）。常见原因：模型名不存在/未开通"
                    "（检查设置中的模型名称，DeepSeek 官方用 deepseek-chat）、"
                    "或接口被限流静默返回空。")
            return content
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            err_text = f"{exc.code} {detail}"
            # 400 且疑似"未知 thinking 参数/缓存字段/response_format 不支持"
            # → 去掉预设、缓存断点与 response_format 重试一次
            if (exc.code == 400 and not _dropped_preset
                    and any(k in detail.lower() for k in (
                        "enable_thinking", "thinking", "reasoning_effort",
                        "reasoning_split", "cache_control", "cache",
                        "response_format", "json_object", "json mode",
                        "unknown parameter", "invalid parameter", "unknown field"))):
                m2, p2 = _retry_without_preset()
                if p2 is not None:
                    model, payload = m2, p2
                    last_error = RuntimeError(err_text)
                    continue  # 走下一次重试
            last_error = RuntimeError(err_text)
            if exc.code < 500:
                break
        except urllib.error.URLError as exc:
            last_error = RuntimeError(f"无法连接 AI 接口：{exc.reason}")
        except (KeyError, IndexError, TypeError) as exc:
            last_error = RuntimeError("AI 接口响应格式不兼容。")
            break
        if attempt < retries:
            delay = _retry_sleep(attempt)
            LOG.warning("AI 调用第 %d 次失败，%.1fs 后重试...", attempt + 1, delay)

    record(route=route, model=model, ok=False,
           error_kind=type(last_error).__name__ if last_error else "unknown",
           start=start)
    raise last_error if last_error else RuntimeError("AI 调用失败")


def call_ai_vision(text: str, image_data_uri: str, max_tokens: int = 900) -> str:
    """B1 视觉识别：以 image_url 数据 URI 调用配置的 vision 模型（OpenAI 兼容）。"""
    config = get_cached_settings()
    model = config.get("vision_model", "").strip() or config.get("model", "").strip()
    if not model:
        raise ValueError("未配置视觉模型，请在「AI 设置」中填写 vision_model")
    messages = [{"role": "user", "content": [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": image_data_uri}},
    ]}]
    return call_ai(messages, max_tokens=max_tokens, tier="heavy", model_override=model)


def embed(texts: list[str], model: str | None = None) -> list[list[float]]:
    """M8 可选向量检索：调用配置的 /v1/embeddings（OpenAI 兼容，本地 Ollama 同样支持）。

    未配置 embedding_model 时抛 ValueError，由 rag.py 捕获后降级为纯 BM25（不阻断摄取/检索）。
    返回向量列表与输入 texts 一一对应。
    """
    if not texts:
        return []
    config = get_cached_settings()
    model = (model or config.get("embedding_model", "")).strip() or config.get("model", "").strip()
    if not model:
        raise ValueError("未配置 embedding 模型，请在「AI 设置」中填写 embedding_model")
    base = (config.get("api_base", "") or "").strip()
    if not base:
        raise ValueError("未配置 API 地址")
    key = config.get("api_key", "")
    if not key and not is_local_endpoint(base):
        raise ValueError("未配置 API 密钥")
    url = base.rstrip("/") + "/embeddings"
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = json.dumps({"model": model, "input": texts}).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"embedding 请求失败 HTTP {exc.code}") from exc
    except Exception as exc:
        raise RuntimeError(f"embedding 接口不可达: {exc}") from exc
    items = sorted(data.get("data") or [], key=lambda x: x.get("index", 0))
    vecs: list[list[float]] = []
    for it in items:
        emb = it.get("embedding")
        if not emb:
            raise RuntimeError("embedding 响应缺少 embedding 字段")
        vecs.append([float(x) for x in emb])
    if len(vecs) != len(texts):
        raise RuntimeError("embedding 返回数量与输入不一致")
    return vecs


def call_ai_stream(
    messages: list[dict[str, str]],
    max_tokens: int = 700,
    tier: str | None = None,
    route: str = "",
) -> Any:
    """C1 流式调用：请求 stream=true，返回 (生成器逐块文本, 超时重试语义)。

    生成器每步产出增量文本；调用方负责关闭响应。
    """
    from telemetry import record
    model, url, payload, headers, start = _prepare_ai_request(
        messages, max_tokens, tier, route, stream=True,
    )
    _check_ai_target(url)  # 与非流式同一出站边界校验
    request = urllib.request.Request(
        url, data=payload, headers=headers, method="POST",
    )
    try:
        response = _OPENER.open(request, timeout=120)
    except Exception as exc:
        record(route=route, model=model, ok=False,
               error_kind=type(exc).__name__, start=start)
        raise

    def _chunks():
        usage_seen: dict[str, Any] = {}
        saw_content = False
        piece: dict[str, Any] = {}
        try:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    piece = json.loads(data)
                    delta = piece["choices"][0]["delta"].get("content", "")
                except (KeyError, IndexError, TypeError, json.JSONDecodeError):
                    # 兼容各家的最后一块 usage 汇总（choices 为空）
                    try:
                        if isinstance(piece, dict) and piece.get("usage"):
                            usage_seen.update(piece["usage"])
                    except Exception:
                        pass
                    delta = ""
                if delta:
                    saw_content = True
                    yield delta
            # reasoner 模型整段只有推理、无正文 → 明确报错（调用方降级），不返回空白
            if not saw_content:
                raise RuntimeError(
                    "AI 流式仅返回了推理内容、未生成最终答案（reasoning_content 非空而 "
                    "content 为空）。建议：使用非推理模型，或在配置中提高 max_tokens。")
            record(route=route, model=model, ok=True, start=start,
                   tokens=int(usage_seen.get("total_tokens") or 0),
                   cached=_cached_tokens(usage_seen))
        except Exception as exc:
            record(route=route, model=model, ok=False,
                   error_kind=type(exc).__name__, start=start)
            raise
        finally:
            response.close()

    return _chunks()


def runtime_key_present() -> bool:
    """会话内是否已设置运行时密钥（供上层 ai_configured 兜底判断）。"""
    return bool(_runtime_key)
