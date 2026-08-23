"""AI 调用层：OpenAI 兼容接口、提示词构造、降级提示。"""
from __future__ import annotations

import ipaddress
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
            return val
        _result_mem.pop(k, None)
    try:
        from db import DB_LOCK, db
        from validate import SchemaError as _SE
        with DB_LOCK, db() as conn:
            row = conn.execute(
                "SELECT payload, created_at FROM ai_result_cache WHERE cache_key = ?", (k,)).fetchone()
        if row:
            val = json.loads(row["payload"])
            _result_mem[k] = (time.time(), val)
            _inc_cache(True)
            return val
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
        "temperature": eff.get("temperature") or "0.3",
        "default_subject": eff.get("default_subject", "physics"),
        "hint_cache_enabled": eff.get("hint_cache_enabled", "1") != "0",
        "daily_review_cap": int(eff.get("daily_review_cap", "0") or 0),
        "ai_context_tokens": int(eff.get("ai_context_tokens", "32000") or 32000),
        "allow_local_ai": eff.get("allow_local_ai", "1") != "0",
        "disable_thinking": eff.get("disable_thinking", "1") != "0",
        "max_output_tokens": int(eff.get("max_output_tokens", "4096") or 4096),
        "has_api_key": has_key,
        "key_source": eff.get("key_source", "none"),
        # 存在 keys.enc 但当前未解锁（key_source 为 none/runtime）时提示可解锁
        "key_file_locked": bool(key_file_exists()) and not has_key,
    }


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
    return max(0.0, min(2.0, value))


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


def _prepare_ai_request(
    messages: list[dict[str, str]],
    max_tokens: int,
    tier: str | None,
    route: str,
    stream: bool,
    model_override: str | None = None,
    skip_preset: bool = False,
    temperature: float | None = None,
) -> tuple[str, str, dict[str, str], dict[str, str], float]:
    """call_ai / call_ai_stream 共用的配置校验与请求构造。

    返回 (model, url, payload, headers, start)；未配置时记录 telemetry 并抛 ValueError。
    skip_preset=True 时不注入模型预设参数（用于 400 未知参数后的降级重试）。
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
                          else max(0.0, min(2.0, float(temperature))),
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
) -> str:
    """调 OpenAI 兼容接口。temperature 覆盖设置；None=用全局配置。"""
    from telemetry import record
    model, url, payload, headers, start = _prepare_ai_request(
        messages, max_tokens, tier, route, stream=False, model_override=model_override,
        temperature=temperature,
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
            LOG.warning("AI 请求 400：去掉模型预设 thinking 参数后重试。")
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
            # 400 且疑似"未知 thinking 参数"→ 去掉预设重试一次
            if (exc.code == 400 and not _dropped_preset
                    and any(k in detail.lower() for k in (
                        "enable_thinking", "thinking", "reasoning_effort",
                        "reasoning_split", "unknown parameter", "invalid parameter"))):
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
            LOG.warning("AI 调用第 %d 次失败，正在重试...", attempt + 1)

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


# ─────────────────────────────────────────────────────────────
# 学科感知导师人格（subject-aware tutor personas）  [v1]
# 原硬编码的"物理"人格改为按 subject 取模板；未知学科回落 generic（中性），
# 不再一律物理化。physics 档完整保留原始措辞，既有物理流程不变。
# ─────────────────────────────────────────────────────────────

_SUBJECT_DISPLAY = {
    "physics": "物理", "chemistry": "化学", "math": "数学", "biology": "生物",
    "history": "历史", "geography": "地理", "cs": "计算机", "programming": "编程",
    "english": "英语", "chinese": "语文", "default": "该学科",
}
_SUBJECT_ALIASES = {
    "大学物理": "physics", "物理": "physics", "电磁学": "physics", "力学": "physics",
    "热学": "physics", "光学": "physics", "原子物理": "physics",
    "电磁感应": "physics", "电磁场": "physics", "量子力学": "physics", "电动力学": "physics",
    "化学": "chemistry", "大学化学": "chemistry", "有机化学": "chemistry", "无机化学": "chemistry",
    "化学反应": "chemistry", "化学平衡": "chemistry",
    "数学": "math", "高等数学": "math", "线性代数": "math", "微积分": "math",
    "导数": "math", "概率论": "math", "离散数学": "math", "解析几何": "math",
    "生物": "biology", "生物学": "biology",
    "历史": "history", "世界史": "history",
    "地理": "geography",
    "计算机": "cs", "编程": "programming", "程序": "programming", "代码": "programming",
    "英语": "english",
    "语文": "chinese", "中文": "chinese",
}


def _subject_display(subject: str) -> str:
    return _SUBJECT_DISPLAY.get((subject or "").strip().lower(), _SUBJECT_DISPLAY["default"])


def _resolve_subject(*candidates: str) -> str:
    """从 subject / course / topic 候选中解析学科键；未知 → ''（回落 generic）。"""
    known = set(_SUBJECT_DISPLAY) - {"default"}
    for c in candidates:
        c = (c or "").strip().lower()
        if c in known:
            return c
        if c in _SUBJECT_ALIASES:
            return _SUBJECT_ALIASES[c]
    return ""


# 中性通用子组件（非物理学科复用，避免物理化措辞）
_GENERIC_STAGE_LABELS = {"concept": "核心概念", "premise": "前提条件", "counterexample": "反例辨析",
                         "extreme": "边界推演", "verify": "检验设计"}
_GENERIC_STAGE_PROMPTS = {
    "concept": '请先用直观的方式解释「{topic}」的核心含义，尽量少用术语。',
    "premise": "这个结论成立需要哪些前提？请至少说出两个。",
    "counterexample": "请给出一个容易误用该概念的反例，并解释错在哪里。",
    "extreme": "如果某个关键参数趋近于极端值，结果应该怎样变化？",
    "verify": "请设计一种方法来检验你刚才的解释。",
}
_GENERIC_DEEPER_PROMPTS = {
    "concept": "你的解释还停在表面。请换一个角度重新描述一次，只说核心过程，不要堆术语。",
    "premise": "前提说得不完整。请再补一个容易被忽略的适用条件，并说明违反它会出现什么错误结果。",
    "counterexample": "这个反例说服力不够。请换成「{topic}」最容易误用的一处边界情况，指出误用者错在哪一步。",
    "extreme": "请具体说明：参数趋于极端时哪个量变化最剧烈、哪个量趋于稳定，各有什么意义。",
    "verify": "请给出关键的观测/验证量、预期范围，以及怎样排除干扰。",
}
_GENERIC_WEAK_PATTERNS = [
    (re.compile(r"适用条件|前提|假设"), "适用条件/前提交代不全"),
    (re.compile(r"公式|代入|计算|推导"), "推导与计算不够严谨"),
    (re.compile(r"反例|误用|边界"), "概念边界与反例辨析不足"),
    (re.compile(r"极限|边界|趋于|极端"), "边界/极端情形分析不完整"),
    (re.compile(r"验证|检验|实验|测量"), "检验/验证设计思路不足"),
]
_GENERIC_ERR_CHECK = {
    "概念理解错误": "先帮学生核对核心概念与适用条件，指出概念误区，不直接给答案。",
    "计算错误": "请复核运算过程，指出第几步可能出错并引导重算。",
    "粗心笔误": "提示按步骤检查符号、单位和抄写，提醒这类失误最容易在细节处。",
    "时间压力": "提醒先建立最少必要步骤的解题顺序，并给出抢分优先级建议。",
    "审题错误": "指出题目中容易被忽略的条件和关键词，引导重新读题。",
    "公式/事实空白": "给出关键公式或事实的适用边界与推导线索，帮助回忆而非背诵。",
    "直觉陷阱": "提示先用特例或极限检验直觉，指出反直觉点，不要直接判对错。",
}

# 物理档（BASE）：完整保留原始措辞，保证既有物理流程不变
_BASE_PROFILE: dict[str, Any] = {
    "ta_zh": "你是严格而耐心的大学物理助教。你的任务是促进主动学习，不是替学生交作业。"
             "优先检查物理模型、适用条件、量纲、边界条件和极限情况。使用中文和清晰的 LaTeX。",
    "ta_en": "You are a strict but patient university physics TA. Your job is to promote active "
             "learning, not to do the homework for the student. "
             "Prioritize checking the physical model, applicable conditions, dimensions, "
             "boundary conditions and limiting cases. Answer in English with clear LaTeX.",
    "tag_extractor": "你是物理题标签提取器。根据题目提取 3-6 个标签，每项格式必须是 "
                     "'知识点:名称'、'题型:名称'、'难度:易|中|难'、'方法:名称'、'错因:名称' 之一。"
                     "只返回 JSON，不要多余文字。结构为 {tags: [...], confidence: 0-1}。\n"
                     "参考示例：题目：求斜面上物体沿 30° 角下滑的加速度。"
                     "输出：{\"tags\": [\"知识点:斜面受力分析\", \"题型:计算\", \"难度:中\", "
                     "\"方法:受力分解\", \"错因:摩擦力方向\"], \"confidence\": 0.92}",
    "variant_author": "你是物理出题助手。基于给定错题生成 3 道变式，三题模式分别为：数值替换、情境替换、反向设问。"
                      "每题必须包含 mode、title、content、answer 四个字段，只返回 JSON。可选补充 type（single/multiple/fill/subjective/composite，缺省开放式 subjective）与 choices（选择题选项数组）；选择题 answer 填正确选项字母。",
    "feynman_novice": "向一位完全不懂物理的新手讲解。规则：不许读公式，只讲物理图像与直觉。"
                      "讲完直接发送，我会帮你对照标准解析找漏点。",
    "oral_teacher": "你是严格的大学物理口试老师（苏格拉底式）。",
    "oral_teacher_summary": "你是严格的大学物理口试老师。这是最后一轮总结。",
    "oral_card_author": "你是物理老师。基于口试记录生成一道复习题的题目正文（一段话），"
                        "围绕薄弱点设计，要求学生用自己的话重述概念并辨析反例。只输出题目正文，不要其他内容。",
    "stage_labels": {"concept": "物理图像", "premise": "前提条件", "counterexample": "反例辨析",
                     "extreme": "极限推演", "verify": "检验设计"},
    "stage_prompts": {
        "concept": '请不用公式，先用物理图像解释「{topic}」的核心含义。',
        "premise": "这个结论成立需要哪些前提？请至少说出两个。",
        "counterexample": "请给出一个容易误用该概念的反例，并解释错在哪里。",
        "extreme": "如果某个关键参数趋近于零或无穷大，结果应该怎样变化？",
        "verify": "请设计一种实验或数值方法来检验你刚才的解释。",
    },
    "deeper_prompts": {
        "concept": "你的解释还停在表面。请从力的来源或能量角度重新描述一次，不许用公式，只说物理过程。",
        "premise": "前提说得不完整。请再补一个容易被忽略的适用条件，并说明违反它会出现什么错误结果。",
        "counterexample": "这个反例说服力不够。请换成「{topic}」最容易误用的一处边界情况，指出误用者错在哪一步。",
        "extreme": "请具体说明：参数趋于极限时哪个量发散、哪个量饱和，各有什么物理意义。",
        "verify": "请给出实验的关键观测量、预期数值范围，以及怎样排除系统误差。",
    },
    "weak_patterns": [
        (re.compile(r"适用条件|前提|假设"), "适用条件/前提交代不全"),
        (re.compile(r"公式|代入|计算"), "公式推导与代入计算不够严谨"),
        (re.compile(r"反例|误用|边界"), "概念边界与反例辨析不足"),
        (re.compile(r"量纲|极限|趋于"), "量纲/极限分析不完整"),
        (re.compile(r"实验|验证|测量"), "检验/实验设计思路不足"),
    ],
    "err_check": {
        "概念理解错误": "先帮学生核对物理模型与适用条件，指出概念误区，不直接给公式。",
        "计算错误": "请复核运算过程与量纲，指出第几步可能出错并引导重算。",
        "粗心笔误": "提示按步骤检查符号、单位和抄写，提醒这类失误最容易在符号正负号。",
        "时间压力": "提醒先建立最少必要步骤的解题顺序，并给出抢分优先级建议。",
        "审题错误": "指出题目中容易被忽略的条件和关键词，引导重新读题。",
        "公式/事实空白": "给出提示公式的适用边界与推导线索，帮助回忆而非背诵。",
        "直觉陷阱": "提示先用特例或极限检验直觉，指出反直觉点，不要直接判对错。",
    },
    "card_local": "请重新解释该概念的物理图像、适用前提，并给出一个易误用反例。",
}

# 化学档
_CHEM_PROFILE: dict[str, Any] = {
    "ta_zh": "你是严格而耐心的化学助教。你的任务是促进主动学习，不是替学生交作业。"
             "优先检查反应原理、物质结构与性质、方程式配平、条件控制与定量关系。使用中文，必要时用清晰的公式。",
    "ta_en": "You are a strict but patient chemistry tutor. Your job is to promote active learning, "
             "not to do the homework for the student. Prioritize reaction principles, structure and "
             "properties, equation balancing, conditions and stoichiometry. Answer in English.",
    "tag_extractor": "你是化学题标签提取器。根据题目提取 3-6 个标签，每项格式必须是 "
                     "'知识点:名称'、'题型:名称'、'难度:易|中|难'、'方法:名称'、'错因:名称' 之一。"
                     "只返回 JSON，不要多余文字。结构为 {tags: [...], confidence: 0-1}。\n"
                     "参考示例：题目：配平 Fe + O₂ → Fe₂O₃ 并计算电子转移。"
                     "输出：{\"tags\": [\"知识点:氧化还原\", \"题型:配平\", \"难度:中\", "
                     "\"方法:电子守恒\", \"错因:化合价判断\"], \"confidence\": 0.9}",
    "variant_author": "你是化学出题助手。基于给定错题生成 3 道变式，三题模式分别为：数值替换、情境替换、反向设问。"
                      "每题必须包含 mode、title、content、answer 四个字段，只返回 JSON。可选补充 type（single/multiple/fill/subjective/composite，缺省开放式 subjective）与 choices（选择题选项数组）；选择题 answer 填正确选项字母。",
    "feynman_novice": "向一位完全不懂化学的新手讲解。规则：不许堆砌术语，只讲核心直觉与图像。"
                      "讲完直接发送，我会帮你对照标准解析找漏点。",
    "oral_teacher": "你是严格的化学口试老师（苏格拉底式）。",
    "oral_teacher_summary": "你是严格的化学口试老师。这是最后一轮总结。",
    "oral_card_author": "你是化学老师。基于口试记录生成一道复习题的题目正文（一段话），"
                        "围绕薄弱点设计，要求学生用自己的话重述概念并辨析反例。只输出题目正文，不要其他内容。",
    "stage_labels": _GENERIC_STAGE_LABELS,
    "stage_prompts": _GENERIC_STAGE_PROMPTS,
    "deeper_prompts": _GENERIC_DEEPER_PROMPTS,
    "weak_patterns": _GENERIC_WEAK_PATTERNS,
    "err_check": _GENERIC_ERR_CHECK,
    "card_local": "请重新解释该概念的核心图像、适用前提，并给出一个易误用反例。",
}

# 数学档
_MATH_PROFILE: dict[str, Any] = {
    "ta_zh": "你是严格而耐心的数学助教。你的任务是促进主动学习，不是替学生交作业。"
             "优先检查定义、定理适用条件、逻辑推导、计算与边界情况。使用中文和清晰的 LaTeX。",
    "ta_en": "You are a strict but patient mathematics tutor. Your job is to promote active learning, "
             "not to do the homework for the student. Prioritize definitions, theorem conditions, "
             "logical derivation, computation and boundary cases. Answer in English with clear LaTeX.",
    "tag_extractor": "你是数学题标签提取器。根据题目提取 3-6 个标签，每项格式必须是 "
                     "'知识点:名称'、'题型:名称'、'难度:易|中|难'、'方法:名称'、'错因:名称' 之一。"
                     "只返回 JSON，不要多余文字。结构为 {tags: [...], confidence: 0-1}。\n"
                     "参考示例：题目：已知函数 f(x)=x³-3x，求其在区间 [-2,2] 上的最大值。"
                     "输出：{\"tags\": [\"知识点:导数应用\", \"题型:最值\", \"难度:中\", "
                     "\"方法:求导\", \"错因:端点遗漏\"], \"confidence\": 0.93}",
    "variant_author": "你是数学出题助手。基于给定错题生成 3 道变式，三题模式分别为：数值替换、情境替换、反向设问。"
                      "每题必须包含 mode、title、content、answer 四个字段，只返回 JSON。可选补充 type（single/multiple/fill/subjective/composite，缺省开放式 subjective）与 choices（选择题选项数组）；选择题 answer 填正确选项字母。",
    "feynman_novice": "向一位完全不懂数学的新手讲解。规则：不许堆砌术语，只讲核心直觉与图像。"
                      "讲完直接发送，我会帮你对照标准解析找漏点。",
    "oral_teacher": "你是严格的数学口试老师（苏格拉底式）。",
    "oral_teacher_summary": "你是严格的数学口试老师。这是最后一轮总结。",
    "oral_card_author": "你是数学老师。基于口试记录生成一道复习题的题目正文（一段话），"
                        "围绕薄弱点设计，要求学生用自己的话重述概念并辨析反例。只输出题目正文，不要其他内容。",
    "stage_labels": _GENERIC_STAGE_LABELS,
    "stage_prompts": _GENERIC_STAGE_PROMPTS,
    "deeper_prompts": _GENERIC_DEEPER_PROMPTS,
    "weak_patterns": _GENERIC_WEAK_PATTERNS,
    "err_check": _GENERIC_ERR_CHECK,
    "card_local": "请重新解释该定义或定理的核心直觉、适用前提，并给出一个易误用反例。",
}

# 中性通用档（未知学科回落）
_GENERIC_PROFILE: dict[str, Any] = {
    "ta_zh": "你是严格而耐心的学科助教。你的任务是促进主动学习，不是替学生交作业。"
             "优先检查核心概念、适用条件、逻辑与边界情况。使用中文，必要时用清晰的公式或 LaTeX。",
    "ta_en": "You are a strict but patient subject tutor. Your job is to promote active learning, "
             "not to do the homework for the student. Prioritize checking core concepts, applicable "
             "conditions, logic and boundary cases. Answer in English, using clear formulas or LaTeX when needed.",
    "tag_extractor": "你是题目标签提取器。根据题目提取 3-6 个标签，每项格式必须是 "
                     "'知识点:名称'、'题型:名称'、'难度:易|中|难'、'方法:名称'、'错因:名称' 之一。"
                     "只返回 JSON，不要多余文字。结构为 {tags: [...], confidence: 0-1}。\n"
                     "参考示例：题目：求 y=x² 在 x=1 处的导数。"
                     "输出：{\"tags\": [\"知识点:导数\", \"题型:计算\", \"难度:易\", \"方法:幂函数求导\"], "
                     "\"confidence\": 0.95}",
    "variant_author": "你是出题助手。基于给定错题生成 3 道变式，三题模式分别为：数值替换、情境替换、反向设问。"
                      "每题必须包含 mode、title、content、answer 四个字段，只返回 JSON。可选补充 type（single/multiple/fill/subjective/composite，缺省开放式 subjective）与 choices（选择题选项数组）；选择题 answer 填正确选项字母。",
    "feynman_novice": "向一位完全不懂这个主题的新手讲解。规则：不许堆砌术语，只讲核心直觉与图像。"
                      "讲完直接发送，我会帮你对照标准解析找漏点。",
    "oral_teacher": "你是严格的口试老师（苏格拉底式）。",
    "oral_teacher_summary": "你是严格的口试老师。这是最后一轮总结。",
    "oral_card_author": "你是老师。基于口试记录生成一道复习题的题目正文（一段话），"
                        "围绕薄弱点设计，要求学生用自己的话重述概念并辨析反例。只输出题目正文，不要其他内容。",
    "stage_labels": _GENERIC_STAGE_LABELS,
    "stage_prompts": _GENERIC_STAGE_PROMPTS,
    "deeper_prompts": _GENERIC_DEEPER_PROMPTS,
    "weak_patterns": _GENERIC_WEAK_PATTERNS,
    "err_check": _GENERIC_ERR_CHECK,
    "card_local": "请重新解释该概念的核心图像、适用前提，并给出一个易误用反例。",
}

_SUBJECT_OVERRIDES: dict[str, dict[str, Any]] = {
    "chemistry": _CHEM_PROFILE,
    "math": _MATH_PROFILE,
}


def _subject_profile(subject: str = "") -> dict[str, Any]:
    """返回学科感知导师人格；physics 原样、chem/math 专属、未知回落中性通用。"""
    key = _resolve_subject(subject)
    if not key or key == "physics":
        return _BASE_PROFILE
    override = _SUBJECT_OVERRIDES.get(key)
    if not override:
        return _GENERIC_PROFILE
    return {**_BASE_PROFILE, **override}


def problem_prompt(problem: dict[str, Any], level: int, lang: str = "zh") -> list[dict[str, str]]:
    level_rules = {
        1: "只指出应该检查的概念或最早可能出错的位置，不给公式答案，最多100字。",
        2: "给出解题方向和关键关系，但留出至少一个关键步骤让学生完成，最多180字。",
        3: "给出较完整的解题框架、检查方法和下一步，但不要代写最终作业，最多300字。",
        4: "给出完整解析：列清步骤、关键公式与最终结果，但最后补一句『自己重做一遍』的引导，最多400字。",
    }
    level_rules_en = {
        1: "Only point out the concept to check or the earliest likely error location; no formula answers, at most 100 words.",
        2: "Give the solving direction and key relations, but leave at least one key step for the student to finish, at most 180 words.",
        3: "Give a fairly complete solving framework, checking methods and next steps, but do not write the final answer for the student, at most 300 words.",
        4: "Give the full solution: list the steps, key formulas and final result, but end with a prompt to redo it yourself, at most 400 words.",
    }
    p = _subject_profile(problem.get("subject", ""))
    # C6：按错因定向检查（提高提示针对性）
    error_line = ""
    try:
        from errors import ERROR_TYPE_LABELS, normalize_error_type
        et = normalize_error_type(problem.get("error_type"))
        if et in ERROR_TYPE_LABELS:
            error_line = f"（该学生标记的错因：{ERROR_TYPE_LABELS[et]}。针对性要求：{p['err_check'][ERROR_TYPE_LABELS[et]]}）"
    except Exception as exc:
        LOG.debug("错因提示构造失败（可忽略）: %s", exc)
    # C5：AI 请求上下文附加学习者档案（隐私仅本地；失败不影响主流程）
    profile_line = ""
    try:
        from profile import snapshot
        profile_line = snapshot()
    except Exception as exc:
        LOG.debug("档案快照生成失败（可忽略）: %s", exc)
    if lang == "en":
        return [
            {
                "role": "system",
                "content": p["ta_en"] + profile_line,
            },
            {
                "role": "user",
                "content": (
                    f"Course: {problem['course']}\nTopic: {problem['topic']}\nProblem: {problem['content']}\n"
                    f"My attempt: {problem['my_attempt'] or 'Not provided yet'}\n"
                    f"Level {level} hint. Requirement: {level_rules_en[level]}"
                ),
            },
        ]
    return [
        {
            "role": "system",
            "content": p["ta_zh"] + profile_line,
        },
        {
            "role": "user",
            "content": (
                f"课程：{problem['course']}\n知识点：{problem['topic']}\n题目：{problem['content']}\n"
                f"我的尝试：{problem['my_attempt'] or '尚未提供'}\n"
                f"{error_line}\n"
                f"这是第 {level} 级提示。要求：{level_rules[level]}"
            ),
        },
    ]


def fallback_hint(problem: dict[str, Any], level: int, lang: str = "zh") -> str:
    """离线兜底提示：AI 不可用时的降级路径（本地无密钥默认模式）。

    按学科取提示：物理或未知学科回落原物理措辞（保持既有测试与物理流程不变）；
    其他学科用中性通用提示，不再出现「物理模型 / 量纲 / 受力图」等专属词。
    """
    topic = problem.get("topic") or ("这个问题" if lang == "zh" else "this problem")
    attempt = problem.get("my_attempt", "").strip()
    course = (problem.get("course") or "").strip()
    sbj = _resolve_subject(problem.get("subject", ""), course, topic)

    # 物理 / 未知学科：保留原措辞（向后兼容既有物理测试与流程）
    if sbj in ("physics", ""):
        if lang == "en":
            extra = ("You have not recorded your attempt yet — first draw a diagram or write the basic equations."
                     if not attempt else "Start from the first basic equation you wrote and label each term's source and sign.")
            if level == 1:
                return (f"Do not calculate yet. Clarify the object of study, the known and unknown quantities, and the "
                        f"applicable conditions for \"{topic}\"; then check that every equation you wrote is "
                        f"dimensionally consistent.")
            if level == 2:
                return (f"Break the problem into: build a model → choose coordinates / conserved quantities → write the "
                        f"basic equations → check boundary conditions. {extra}")
            if level == 3:
                return ("Write the minimal set of equations, solve symbolically first, then plug in numbers. "
                        "Do a triple check with dimensions, special limits and orders of magnitude; add the exact step "
                        "where you get stuck to \"My Attempt\".")
            return (f"Full solution framework: 1) identify the object and known/unknown quantities; "
                    f"2) choose the physical model ({topic}) and state its applicable conditions; "
                    "3) write the basic equations and check signs and dimensions line by line; "
                    "4) solve symbolically first, then substitute numbers; "
                    "5) verify with limiting cases (mass→0, force→∞) and orders of magnitude. "
                    "Compare key formulas and the final result with a standard solution — redo it yourself before checking.")

        _TOPIC_HINTS: dict[str, tuple[str, str, str]] = {
            "力学": (
                f"先给「{topic}」中的每个物体画受力图，标出全部受力与正方向，再检查是否遗漏了约束反力或摩擦力。",
                "从你写出的第一条基本方程开始，逐项标注来源（牛顿第二定律 / 动量守恒 / 动能定理）和正方向。" if attempt else "画完受力图后写出每个物体的运动方程；检查坐标系是否统一、质量是否区分。",
                "列出牛顿定律或守恒律的最小方程组，先求符号表达式，再代数值。最后检查极限情况（质量→0、外力→∞）和量纲。",
            ),
            "电磁学": (
                f"先明确「{topic}」中的电荷分布 / 电流构型 / 磁场源，画出场线或等效电路图；再检查对称性和适用条件。",
                "从高斯定理或安培环路定律出发，标注哪个面对称/轴对称；区分电场 E 和磁场 B 的方向。" if attempt else "画出电场线或磁感线分布；如果你在用电路模型，请确认每个元件两端的电压符号和参考方向。",
                "写出麦克斯韦方程组中对应的积分/微分形式，代入对称条件化简。最后检查边界条件（导体表面/介质界面）和极限情况。",
            ),
            "热学": (
                f"先明确「{topic}」中的系统边界、状态参量(P,V,T)和过程类型（等温/绝热/等压/等容）。",
                "从状态方程 PV=nRT 或第一定律 dU=δQ-δW 出发，逐项确认符号正负约定。" if attempt else "写出系统初末态的热力学参量，确认过程的可逆性和做功表达式。",
                "联立状态方程与能量方程，先求符号表达式，再代入数值。最后检查极限情况（体积→∞/→0）和量纲。",
            ),
            "光学": (
                f"先画出「{topic}」中的光路图，标记入射角、折射角、光程差；确认所用原理（几何光学/波动光学）。",
                "从折射定律或干涉条件出发，确认符号约定（实正虚负）和介质折射率。" if attempt else "画出完整光路图，标注各界面处的入射角和透射角，写下每个界面的折射/反射方程。",
                "联立光学路径方程组，先求符号表达式，检查薄透镜近似或傍轴条件是否成立。最后验算极限情况（折射率→1 退化为真空）。",
            ),
            "振动与波": (
                f"先明确「{topic}」的振动模型（简谐/阻尼/受迫）和初始条件；画出振动曲线或波的传播示意图。",
                "从运动方程 x''+ω²x=0 或波动方程出发，确认边界条件和初始相位。" if attempt else "写出系统的运动微分方程，判断是简谐振动还是阻尼振动；检查初始位移和初始速度。",
                "求解微分方程得通解 + 特解，代初条件定常数。检查极限情况（阻尼→∞ 过阻尼不振荡）和量纲。",
            ),
        }

        keywords = _TOPIC_HINTS.get(course.split("（")[0].split("(")[0].strip())
        if not keywords:
            for key, vals in _TOPIC_HINTS.items():
                if key in course or key in topic:
                    keywords = vals
                    break

        if keywords and level <= len(keywords):
            return keywords[level - 1]

        if level == 1:
            return f'先不要计算。请明确「{topic}」中研究对象、已知量、未知量与成立条件；再检查你的每个等式是否量纲一致。'
        if level == 2:
            extra = "你还没有记录自己的尝试，请先画出受力图或基本方程。" if not attempt else "从你写出的第一条基本方程开始，逐项标注来源和正方向。"
            return f"建议把问题拆成：建立模型 → 选择坐标/守恒量 → 写基本方程 → 检查边界条件。{extra}"
        if level == 3:
            return '请写出最小方程组，先求符号表达式，再代入数值。最后用量纲、特殊极限和数量级做三重检查；把仍卡住的具体一步补充到「我的尝试」中。'
        return (f'完整解析框架：1) 明确研究对象与已知/未知量；2) 选择物理模型（{topic}）并写适用条件；'
                '3) 列基本方程并逐项检查符号与量纲；4) 解出符号表达式再代数值；'
                '5) 用极限情况（质量→0、外力→∞）与数量级复核。'
                '关键公式与最终结果请对照标准解析核对——建议先自己重做一遍再对答案。')

    # 非物理学科：中性通用提示（不出现物理专属词）
    if lang == "en":
        extra = ("You have not recorded your attempt yet — first outline the main steps or write down the key relations."
                 if not attempt else "Start from the first step you wrote and label each term's source.")
        if level == 1:
            return (f"Do not calculate yet. Clarify the object of study, the known and unknown quantities, and the "
                    f"conditions or assumptions for \"{topic}\"; then check that each step is self-consistent.")
        if level == 2:
            return (f"Break the problem into: understand the statement → list knowns/unknowns → "
                    f"establish the key relations → check the premises. {extra}")
        if level == 3:
            return ("Write the minimal set of solution steps, solve symbolically first if possible, then plug in numbers. "
                    "Do a check with special cases, limits and orders of magnitude; add the exact step "
                    "where you get stuck to \"My Attempt\".")
        return (f"Full solution framework: 1) identify the object and known/unknown quantities; "
                f"2) choose the applicable model or method ({topic}) and state its premises; "
                "3) write the key relations and check them line by line; "
                "4) solve symbolically first, then substitute numbers; "
                "5) verify with special cases and orders of magnitude. "
                "Compare key results with a standard solution — redo it yourself before checking.")

    # 非物理 · 中文
    if level == 1:
        return f'先不要计算。请明确「{topic}」的研究对象、已知量与未知量，以及需要注意的前提或适用条件；再检查每一步推导是否自洽、是否遗漏关键量。'
    if level == 2:
        extra = "你还没有记录自己的尝试，请先梳理题意、列出已知与未知。" if not attempt else "从你写出的第一步开始，逐项标注依据与来源。"
        return f"建议围绕「{topic}」把问题拆成：理解题意 → 列出已知/未知 → 建立关键关系 → 检查前提条件。{extra}"
    if level == 3:
        return '请写出最精简的求解步骤，先求符号或通式再代入具体值。最后用特例、极限或数量级做检查；把仍卡住的具体一步补充到「我的尝试」中。'
    return (f'完整求解框架：1) 明确研究对象与已知/未知量；2) 选择适用的模型或方法（{topic}）并写清前提；'
            '3) 列关键关系并逐项检查；4) 先求符号表达式再代数值；'
            '5) 用特例或数量级复核。'
            '关键结果与标准解析对照——建议先自己重做一遍再对答案。')


# ── B5 自动标签 + 知识提取 ──────────────────────────────────

_TAG_SCHEMA = {
    "tags": {"type": "array", "items": {"type": "string"}, "required": True},
    "confidence": {"type": "number", "min": 0.0, "max": 1.0, "required": True},
}

# 降级词库：物理知识点关键词（按题面出现次数加权）
_KNOWLEDGE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "力学": ("牛顿", "受力", "摩擦力", "动量", "冲量", "动能", "机械能", "圆周", "平抛", "斜面", "弹簧", "质心", "角动量", "转动惯量", "简谐"),
    "电磁学": ("电场", "电势", "电容", "电流", "电阻", "欧姆", "磁感应", "安培", "洛伦兹", "法拉第", "电磁感应", "楞次", "麦克斯韦", "电感", "LC"),
    "热学": ("热力学", "温度", "压强", "内能", "熵", "绝热", "等温", "等压", "等容", "热机", "卡诺", "分子动理论", "理想气体"),
    "光学": ("折射", "反射", "衍射", "干涉", "偏振", "光程", "透镜", "全反射", "波长", "频率", "光子"),
    "振动与波": ("振动", "波动", "波速", "波长", "驻波", "多普勒", "相位", "简谐"),
    "原子物理": ("原子", "核", "能级", "跃迁", "衰变", "半衰期", "光子", "光电效应", "波尔", "量子"),
}
_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "计算题": ("求", "计算", "数值", "解得", "大小为", "等于"),
    "概念题": ("概念", "区别", "判断", "说法正确的是", "错误的是", "为什么", "原理"),
    "证明题": ("证明", "推导", "验证", "证明题"),
    "实验题": ("实验", "测量", "器材", "误差", "读数"),
}
_METHOD_KEYWORDS: dict[str, tuple[str, ...]] = {
    "受力分析": ("受力", "隔离", "整体法"),
    "守恒法": ("守恒", "能量守恒", "动量守恒"),
    "微积分": ("积分", "微分", "dt", "ds"),
}


def local_tags(title: str, content: str, course: str = "", topic: str = "", subject: str = "") -> dict[str, Any]:
    """B5 降级：关键词规则打标签（无 AI 时可用）。置信度按匹配强度估算。

    物理或未知学科沿用原物理分科词库；其他学科仅用通用题型/课程/知识点标签，
    不再套用物理分科关键词（避免化学/数学/历史题被打上「电磁学」等物理标签）。
    """
    sbj = _resolve_subject(subject, course, topic)
    text = f"{title}\n{content}"
    found: list[str] = []
    hits = 0
    if course:
        found.append(f"课程:{course}")
        hits += 1
    if topic:
        found.append(f"知识点:{topic}")
        hits += 1
    if sbj in ("physics", ""):
        for label, words in _KNOWLEDGE_KEYWORDS.items():
            count = sum(text.count(w) for w in words)
            if count:
                found.append(f"知识点:{label}")
                hits += count
        for label, words in _METHOD_KEYWORDS.items():
            if any(w in text for w in words):
                found.append(f"方法:{label}")
                hits += 1
    for label, words in _TYPE_KEYWORDS.items():
        if any(w in text for w in words):
            found.append(f"题型:{label}")
            hits += 1
    if not found:
        found.append("知识点:待分类")
    confidence = min(0.95, 0.45 + 0.08 * hits) if hits > 0 else 0.3
    return {"tags": found, "confidence": round(confidence, 2), "source": "local"}


def explain_concept(
    name: str,
    subject: str = "physics",
    aliases: str = "",
    prereq: str = "",
    succ: str = "",
    contrast: str = "",
) -> str:
    """生成概念详解（词条式释义），纯 AI，不落库；由前端编辑后决定是否保存为 explanation。

    失败抛 RuntimeError（上层 handler 捕获降级）；无 AI 配置时调用方应已通过 _ai_quota 拦截。
    """
    ctx = []
    if aliases.strip():
        ctx.append(f"别名/缩写：{aliases.strip()}")
    if prereq.strip():
        ctx.append(f"前置概念：{prereq.strip()}")
    if succ.strip():
        ctx.append(f"后继概念：{succ.strip()}")
    if contrast.strip():
        ctx.append(f"对比概念：{contrast.strip()}")
    ctx_block = "\n".join(ctx) if ctx else "（无额外上下文）"
    prompt = (
        f"你是{subject}学科助教。请为概念「{name}」写一段简明、准确、面向学生的概念详解"
        f"（150-300 字）。要求：\n"
        f"1. 用一句话给出核心定义；\n"
        f"2. 说明其物理/数学含义与直觉；\n"
        f"3. 如有典型应用场景或易错点，简要点出。\n"
        f"不要使用 Markdown 标题，用自然段落。\n"
        f"已知上下文：\n{ctx_block}"
    )
    return call_ai([{"role": "user", "content": prompt}], max_tokens=500, tier="heavy", route="material")


def extract_tags(
    title: str,
    content: str,
    course: str = "",
    topic: str = "",
    subject: str = "",
) -> dict[str, Any]:
    """B5 自动标签：AI 提取（C4 校验）→ 失败自动降级关键词规则。

    返回 {"tags": [...], "confidence": float, "source": "ai"|"local"}。
    仅返回建议，不落库（R3 草稿确认由调用方控制）。
    """
    sbj = _resolve_subject(subject, course, topic)
    p = _subject_profile(sbj)
    user_text = (
        f"课程：{course or '未知'}\n已有知识点：{topic or '无'}\n"
        f"题目标题：{title}\n题目内容：{content}"
    )
    # 结果缓存：同内容标签提取结果稳定 → 命中省 token
    cache_key = f"tags:{sbj}:{title}:{content[:2000]}"
    cached = cache_get(cache_key)
    if cached:
        cached["source"] = "cache"
        return cached
    prompt = [
        {"role": "system", "content": p["tag_extractor"]},
        {"role": "user", "content": user_text},
    ]
    try:
        raw = call_ai(prompt, max_tokens=300, tier="fast", retries=1)
        data = validate_object(raw, _TAG_SCHEMA)
        tags = [str(t).strip() for t in data["tags"] if str(t).strip()]
        if not tags:
            raise SchemaError("标签列表为空")
        confidence = float(data["confidence"])
        if confidence < 0.9:
            return {"tags": tags, "confidence": round(confidence, 2), "source": "ai", "pending": True}
        result = {"tags": tags, "confidence": round(confidence, 2), "source": "ai"}
        cache_set(cache_key, result)
        return result
    except (SchemaError, ValueError) as exc:
        LOG.warning("AI 标签提取校验失败，降级关键词: %s", exc)
        return local_tags(title, content, course, topic, subject)
    except Exception as exc:
        LOG.warning("AI 标签提取失败，降级关键词: %s", exc)
        return local_tags(title, content, course, topic, subject)


# ── A4 举一反三变式题引擎 ─────────────────────────────────

_VARIANT_SCHEMA = {
    "variants": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "required": True},
                "title": {"type": "string", "min_length": 1, "required": True},
                "content": {"type": "string", "min_length": 1, "required": True},
                "answer": {"type": "string", "min_length": 1, "required": True},
                "type": {"type": "string", "enum": ["single", "multiple", "fill", "subjective", "composite"]},
                "choices": {"type": "array", "items": {"type": "string"}},
            },
        },
        "required": True,
    },
}


def local_variants(problem: dict[str, Any], subject: str = "") -> list[dict[str, Any]]:
    """A4 降级：离线参数化变式模板（按错因×题型启发式，零依赖）。"""
    content = problem.get("content", "")
    title = problem.get("title", "")
    topic = problem.get("topic", "")
    error_type = problem.get("error_type", "")
    sbj = _resolve_subject(subject, problem.get("course", ""), topic)
    is_physics = sbj == "physics"
    variants: list[dict[str, Any]] = []
    # 数值替换：把常见整数/小数换成更"丑"的数（计算错场景）
    nums = re.findall(r"\d+(?:\.\d+)?", content)
    if nums:
        swap = {"1": "7", "2": "6", "3": "5", "4": "9", "5": "2", "6": "3", "7": "8", "8": "4", "9": "1"}
        replaced = content
        for n in nums:
            if error_type in ("calculation", "careless"):
                rep = "".join(swap.get(ch, ch) for ch in n)
                replaced = replaced.replace(n, rep, 1)
        dim_note = "量纲与数量级" if is_physics else "单位与数量级"
        variants.append({
            "mode": "数值替换",
            "title": f"{title}（数值变式）",
            "content": replaced,
            "answer": f"同原题解法，注意代入新数值后重新检查{dim_note}。",
        })
    # 情境替换：换一个相近场景（概念/建模错）
    scene_swap = [
        ("斜面", "水平桌面"), ("小车", "木块"), ("小球", "滑块"),
        ("电梯", "火箭"), ("弹簧", "橡皮绳"), ("磁场", "电场"),
    ]
    new_content = content
    for a, b in scene_swap:
        if a in new_content:
            new_content = new_content.replace(a, b, 1)
            variants.append({
                "mode": "情境替换",
                "title": f"{title}（情境变式）",
                "content": new_content,
                "answer": "模型不变，注意新场景下边界条件与关键量的差异。",
            })
            break
    # 反向设问：把"求 X"改为"给 X 反推条件"（概念错场景）
    m = re.search(r"(?:求|计算|大小为|等于)\s*([^，。；]+)", content)
    if m:
        target = m.group(1).strip()
        dim_verify = "验证量纲" if is_physics else "验证单位与数量级"
        variants.append({
            "mode": "反向设问",
            "title": f"{title}（反向设问）",
            "content": f"给定结果 {target} = 已知值，反推题目中某一初始条件；写出推导过程并{dim_verify}。",
            "answer": "把原题正向关系反解为初始条件表达式，代入结果校验。",
        })
    if not variants:
        variants.append({
            "mode": "重述练习",
            "title": f"{title}（重述）",
            "content": f"不看书本，用自己的话重新表述 {topic or title} 的解题思路，并写出关键关系及适用条件。",
            "answer": "对照标准解析检查：模型选择、关系适用条件、边界条件是否完整。",
        })
    return variants


def generate_variants(problem: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """A4 变式生成：AI（C4 校验，不一致不返回）→ 失败降级离线模板。"""
    sbj = _resolve_subject(
        problem.get("subject", ""),
        problem.get("course", ""),
        problem.get("topic", ""),
    )
    p = _subject_profile(sbj)
    prompt = [
        {"role": "system", "content": p["variant_author"]},
        {"role": "user", "content": (
            f"原题：{problem.get('content', '')}\n标题：{problem.get('title', '')}\n"
            f"知识点：{problem.get('topic', '')}\n错因：{problem.get('error_type', '')}"
        )},
    ]
    try:
        raw = call_ai(prompt, max_tokens=1500, tier="heavy", retries=1)
        data = validate_object(raw, _VARIANT_SCHEMA)
        variants = data["variants"]
        if not variants:
            raise SchemaError("变式列表为空")
        return "ai", [{
            "mode": str(v["mode"]).strip(),
            "title": str(v["title"]).strip(),
            "content": str(v["content"]).strip(),
            "answer": str(v["answer"]).strip(),
            **({"type": str(v["type"]).strip()} if v.get("type") else {}),
            **({"choices": [str(c).strip() for c in v["choices"]]} if v.get("choices") else {}),
        } for v in variants]
    except (SchemaError, ValueError) as exc:
        LOG.warning("AI 变式生成校验失败，降级模板: %s", exc)
        return "local", local_variants(problem, sbj)
    except Exception as exc:
        LOG.warning("AI 变式生成失败，降级模板: %s", exc)
        return "local", local_variants(problem, sbj)


# ── A5 按题型出题（题库多题型增强）───────────────────────────

_BANK_Q_TYPES = ("single", "multiple", "fill", "subjective", "composite")

_BANK_Q_SCHEMA = {
    "question": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": list(_BANK_Q_TYPES), "required": True},
            "stem": {"type": "string", "min_length": 5, "required": True},
            "choices": {"type": "array", "items": {"type": "string"}},
            "answer": {"type": "any"},
            "explain": {"type": "string"},
            "parts": {"type": "array", "items": {"type": "object"}},
        },
        "required": True,
    },
}

_BANK_Q_PROMPT = (
    "你是学科题库出题助手。根据用户给的学科、知识点与题型，出 1 道高质量练习题，"
    "严格返回 JSON，结构为 {question: {...}}。字段：\n"
    "- type: 必填，单选=single / 多选=multiple / 填空=fill / 主观=subjective / 大小题=composite\n"
    "- stem: 题干（≥5 字）\n"
    "- choices: 仅 single/multiple 必填，选项数组（≥2 项）\n"
    "- answer: single=正确选项下标(从0)；multiple=正确下标数组；fill=答案字符串或数组(多空)；"
    "subjective=参考答案文本；composite 省略\n"
    "- explain: 解析\n"
    "- parts: 仅 composite 必填，子题数组，每个子题结构同 question（可递归嵌套）\n"
    "只返回 JSON，不要多余文字。\n"
    "参考示例（结构示范，学科与难度可改）：\n"
    '例1（单选）：{"question": {"type": "single", "stem": "两个带同种电荷的小球相互靠近时，作用力方向为？", '
    '"choices": ["互相吸引", "互相排斥", "无作用力", "引力"], "answer": 1, '
    '"explain": "同种电荷相斥。", "concept": "库仑定律"}}\n'
    '例2（大小题）：{"question": {"type": "composite", "stem": "一个物体从静止开始做匀加速直线运动，5s 内位移 25m。", '
    '"parts": [{"type": "single", "stem": "该物体的加速度为？", "choices": ["1 m/s^2", "2 m/s^2", "5 m/s^2", "10 m/s^2"], "answer": 1}, '
    '{"type": "fill", "stem": "第 5s 末的速度为 ____ m/s。", "answer": "10"}], '
    '"explain": "由 s=1/2at^2 得 a=2 m/s^2；v=at=10 m/s。"}}'
)


def local_bank_question(subject: str, topic: str, qtype: str) -> dict[str, Any]:
    """离线降级：产出一道开放式自评占位题（零依赖）。"""
    return {
        "type": "single",
        "stem": f"【{topic or '知识点'}】请用自己的话简述其核心要点并举例说明。",
        "choices": ["能正确表述并举例", "部分正确", "概念混淆", "完全错误"],
        "answer": 0,
        "explain": "离线模式仅提供开放式自评占位，建议联网后重新生成。",
    }


def generate_bank_question(subject: str, topic: str, qtype: str = "single",
                           context: str = "") -> dict[str, Any]:
    """AI 按题型出题，返回与 bank 模型一致的题目 dict；失败降级 local。"""
    if qtype not in _BANK_Q_TYPES:
        qtype = "single"
    p = _subject_profile(subject)
    user_text = (
        f"学科：{subject}\n知识点：{topic or '自选'}\n题型：{qtype}\n"
        f"背景材料：{context or '无'}"
    )
    prompt = [
        {"role": "system", "content": _BANK_Q_PROMPT},
        {"role": "user", "content": user_text},
    ]
    try:
        raw = call_ai(prompt, max_tokens=1500, tier="heavy", retries=1)
        data = validate_object(raw, _BANK_Q_SCHEMA)
        q = data["question"]
        qtype_r = q.get("type")
        if qtype_r not in _BANK_Q_TYPES:
            qtype_r = "single"
        item: dict[str, Any] = {
            "type": qtype_r,
            "stem": str(q.get("stem", "")).strip(),
            "explain": str(q.get("explain", "")).strip(),
        }
        if qtype_r in ("single", "multiple"):
            item["choices"] = [str(c).strip() for c in (q.get("choices") or [])]
            item["answer"] = q.get("answer")
        elif qtype_r in ("fill", "subjective"):
            item["answer"] = q.get("answer")
        elif qtype_r == "composite":
            parts = q.get("parts") or []
            if not parts:
                raise SchemaError("composite 缺少 parts")
            item["parts"] = parts
        return item
    except (SchemaError, ValueError) as exc:
        LOG.warning("AI 出题校验失败，降级: %s", exc)
        return local_bank_question(subject, topic, qtype)
    except Exception as exc:
        LOG.warning("AI 出题失败，降级: %s", exc)
        return local_bank_question(subject, topic, qtype)


# ── A6 AI 审题 ─────────────────────────────────────────────

_REVIEW_SCHEMA = {
    "review": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["pass", "warn", "reject"], "required": True},
            "issues": {"type": "array", "items": {"type": "string"}},
            "comment": {"type": "string"},
            "revised": {"type": "string"},
        },
        "required": True,
    },
}

_REVIEW_PROMPT = (
    "你是严格的教学审题专家。对用户给出的 1 道题做质量审查，出具结论。\n"
    "审题维度（逐项检查后汇总）：\n"
    "1. 题干：是否清晰、无歧义、条件齐全、是否有遗漏或天生无法作答；\n"
    "2. 选择题：答案是否唯一/确定，错误项是否合理、是否有明显凑数；\n"
    "3. 填空/主观题：参考答案是否准确、表述是否严谨；\n"
    "4. 大小题(composite)：各子题是否相互独立、总分与问题是否匹配。\n"
    "只返回 JSON，结构为 {review: {...}}，字段：\n"
    "- verdict: pass(没问题) / warn(有小问题可改进) / reject(有硬伤必须改)\n"
    "- issues: 发现的问题数组（没问题也可给 1-2 条优化建议）\n"
    "- comment: 一句话总评\n"
    "- revised: 若 verdict=reject 给出建议修订后的完整题目 JSON 文本；否则可省略\n"
    "输出示例：{\"review\": {\"verdict\": \"warn\", \"issues\": [\"选项 B 表述有歧义，建议改为更明确的干扰项\"], "
    "\"comment\": \"题目整体合格，仅个别选项需打磨。\"}}"
)

_ai_available: bool | None = None


def ai_configured() -> bool:
    """AI 是否真正可用（能拿到有效密钥或本地端点），而非只看密钥文件是否存在。

    修复：keys.enc 存在但未解锁（口令未设置/错误）时，旧实现仍返回 True，
    导致 UI 显示 AI 可用而实际请求 401/失败、提取报"JSON 解析失败"误导用户。
    现在改为检查合并后的设置是否含非空 api_key（或本地 Ollama 等允许无钥端点）。
    """
    try:
        s = get_cached_settings()
        key = (s.get("api_key") or "").strip()
        if key:
            return True
        base = (s.get("api_base") or "").strip()
        # 本地端点（Ollama 等）允许空密钥
        return bool(base) and is_local_endpoint(base)
    except Exception:
        return bool(_runtime_key)


def review_bank_question(question: dict[str, Any], subject: str = "") -> dict[str, Any]:
    """A6 AI 审题：校验出一题质量。

    返回 {verdict, issues, comment, revised?, ai_available}。
    AI 不可用/异常时降级为 {verdict: "pass", ai_available: False}，不阻断出题流程。
    """
    global _ai_available
    try:
        q = dict(question or {})
        q.setdefault("type", q.get("type") or "single")
        if not ai_configured():
            _ai_available = False
            return {"verdict": "pass", "issues": [], "comment": "", "ai_available": False}
    except Exception:
        _ai_available = False
        return {"verdict": "pass", "issues": [], "comment": "", "ai_available": False}
    payload = json.dumps(q, ensure_ascii=False)
    # 结果缓存：同题同学科审题结果稳定 → 命中直接返回（省 token）
    cache_key = f"review:{subject or ''}:{payload}"
    cached = cache_get(cache_key)
    if cached:
        cached["ai_available"] = True
        cached["cached"] = True
        return cached
    prompt = [
        {"role": "system", "content": _REVIEW_PROMPT},
        {"role": "user", "content": f"学科：{subject or '未知'}\n待审题目：\n{payload}"},
    ]
    try:
        raw = call_ai(prompt, max_tokens=700, tier="heavy", retries=1)
        data = validate_object(raw, _REVIEW_SCHEMA)
        rv = data["review"]
        verdict = rv.get("verdict", "warn")
        if verdict not in ("pass", "warn", "reject"):
            verdict = "warn"
        result = {
            "verdict": verdict,
            "issues": [str(x) for x in (rv.get("issues") or [])][:8],
            "comment": str(rv.get("comment") or "").strip(),
            "revised": str(rv.get("revised") or "").strip() or None,
            "ai_available": True,
        }
        cache_set(cache_key, {k: v for k, v in result.items() if k != "ai_available"})
        return result
    except (SchemaError, ValueError) as exc:
        LOG.warning("AI 审题校验失败，降级: %s", exc)
    except Exception as exc:
        LOG.warning("AI 审题失败，降级: %s", exc)
    _ai_available = False
    return {"verdict": "pass", "issues": [], "comment": "", "ai_available": False}


# ── A7 AI 评分（主观题）────────────────────────────────────

_SCORE_SCHEMA = {
    "score": {
        "type": "object",
        "properties": {
            "score": {"type": "integer", "min": 0, "max": 100, "required": True},
            "comment": {"type": "string"},
            "against": {"type": "string"},
        },
        "required": True,
    },
}

_SCORE_PROMPT = (
    "你是严格、友好的学科阅卷老师。针对用户提供的【题目】【参考答案】【学生作答】，"
    "按 0-100 分给分并点评。\n"
    "评分方式：先对照下面的锚定档位表做匹配（把开放打分转成档位分类，保证一致性），"
    "再在档位区间内微调出具体分：\n"
    "- 90-100 分：全部要点准确覆盖，表述清晰，无实质错误；\n"
    "- 75-89 分：多数要点到位，个别表述不严谨或漏次要点；\n"
    "- 60-74 分：核心要点部分到位，有实质遗漏或关键表述混乱；\n"
    "- 40-59 分：只触及皮毛，多数要点缺失或明显误解；\n"
    "- 0-39 分：跑题、空白或严重错误（须在点评中说明原因并给改进方向）。\n"
    "其他要求：对照参考答案逐要点判分，不苛求措辞一致，抓关键点是否到位；"
    "comment 给出 1-3 句点评（中文）：先总评，再指出失分点与改进建议。\n"
    "只返回 JSON，结构为 {score: {score: 0-100 整数, comment: 点评, against: 命中要点简述}}。\n"
    "参考示例（结构示范）：\n"
    '输入：题目：简述牛顿第二定律。 参考答案：F=ma，F 与 a 同向。 学生：F 等于质量乘加速度。\n'
    '输出：{"score": {"score": 85, "comment": "要点基本到位，但漏了方向关系，建议补充 F 与 a 同向。", '
    '"against": "F=ma"}}\n'
    '输入：题目：简述牛顿第二定律。 参考答案：F=ma，F 与 a 同向。 学生：牛顿第二定律是力学基本定律。\n'
    '输出：{"score": {"score": 30, "comment": "仅复述了定律地位，未给出 F=ma 与方向关系，核心要点缺失。", '
    '"against": "无"}}'
)


def ai_score_item(item: dict[str, Any], user_raw: Any, subject: str = "") -> dict[str, Any]:
    """A7 AI 评分（递归）。

    - 客观题叶（single/multiple/fill）：复用 grade_item 确定性判分，score=100 或 0；
    - 主观题叶（subjective）：AI 可用则调 AI 给 0-100 分＋点评；否则 score=None, needs_review=True；
    - composite：递归聚合，按权重（客观=选项数/空数，主观=100）加权平均；
    - AI 不可用/失败：对应主观标记 needs_review，不拉低总分分母。
    返回 {score(0-100 或 None), ai_available, mode, needs_review, comment?, against?, parts?}。
    """
    from bank import grade_item

    def _res(t: str, score: int | None, mode: str, *, avail: bool = False,
             need: bool = False, weight: int = 1, **extra: Any) -> dict[str, Any]:
        d: dict[str, Any] = {
            "type": t, "score": score, "mode": mode,
            "ai_available": avail, "needs_review": need, "weight": weight,
        }
        d.update(extra)
        return d

    def rec(it: dict[str, Any], ans: Any) -> dict[str, Any]:
        t = it.get("type", "single")
        if t == "composite":
            parts = it.get("parts") or []
            in_ans = ans if isinstance(ans, list) else []
            subs = [rec(p, in_ans[i] if i < len(in_ans) else None)
                    for i, p in enumerate(parts)]
            scored = [s for s in subs if s["score"] is not None]
            if not scored:
                score: int | None = None
            else:
                wsum = sum(s["weight"] for s in scored)
                score = int(round(sum(s["score"] * s["weight"] for s in scored) / (wsum or 1)))
            return _res(
                t, score, _agg_mode([s["mode"] for s in subs]),
                avail=any(s["ai_available"] for s in subs),
                need=any(s["needs_review"] for s in subs),
                weight=1, parts=subs,
            )
        if t == "multiple":
            g = grade_item(it, ans)
            w = max(len(it.get("choices") or []), 1)
            return _res(t, 100 if g["correct"] else 0, "deterministic", weight=w)
        if t == "fill":
            raw_ans = it.get("answer")
            w = len(raw_ans) if isinstance(raw_ans, list) else 1
            g = grade_item(it, ans)
            return _res(t, 100 if g["correct"] else 0, "deterministic", weight=max(w, 1))
        if t == "subjective":
            g = grade_item(it, ans)
            if g["correct"] is not None:  # 理论上有兜底，安全处理
                return _res(t, 100 if g["correct"] else 0, "deterministic")
            out = _ai_subjective(it, ans, subject)
            if out["score"] is None:
                return _res(t, None, "unrated", need=True)
            return _res(t, out["score"], "ai", avail=True, weight=100,
                        comment=out["comment"], against=out.get("against") or "")
        # single / 兜底
        g = grade_item(it, ans)
        return _res(t, 100 if g["correct"] else 0, "deterministic")

    top = rec(item, user_raw)
    out: dict[str, Any] = {
        "score": top["score"],
        "ai_available": top["ai_available"],
        "mode": top["mode"],
        "needs_review": top["needs_review"],
    }
    if item.get("type") == "composite":
        out["parts"] = top["parts"]
    if top.get("comment"):
        out["comment"] = top["comment"]
    if top.get("against"):
        out["against"] = top["against"]
    return out


def _agg_mode(modes: list[str]) -> str:
    if "ai" in modes:
        return "ai" if not any(m != "ai" for m in modes) else "mixed"
    if "unrated" in modes:
        return "unrated"
    return "deterministic"


def _ai_ready() -> bool:
    """AI 是否可用。仅记录观察结果，不缓存 False 以免新录入 key 后无法恢复。"""
    return ai_configured()


def _ai_subjective(item: dict[str, Any], user_raw: Any, subject: str) -> dict[str, Any]:
    """单道主观题 AI 评分。返回 {score(int|None), comment, against}；失败 score=None。"""
    try:
        if not _ai_ready():
            return {"score": None, "comment": "", "against": ""}
    except Exception:
        return {"score": None, "comment": "", "against": ""}
    user_text = str(user_raw or "").strip()
    ref = str(item.get("answer") or "").strip()
    if not user_text:
        # 未作答：保持待评阅语义，不自动给 0 分（避免误把“没写”当“判过”）
        return {"score": None, "comment": "", "against": ""}
    payload = json.dumps({
        "stem": str(item.get("stem") or ""),
        "answer": ref,
        "student": user_text[:2000],
        "subject": subject,
    }, ensure_ascii=False)
    prompt = [
        {"role": "system", "content": _SCORE_PROMPT},
        {"role": "user", "content": f"题目与参考答案与学生作答：\n{payload}"},
    ]
    try:
        # 第 1 次调用（低温 0）：只按 rubric 出结构化分值（评分稳定）
        raw = call_ai(prompt, max_tokens=500, tier="heavy", retries=1,
                      temperature=0.0, route="score")
        data = validate_object(raw, _SCORE_SCHEMA)
        sc = data["score"]
        s = int(sc.get("score") or 0)
        s = max(0, min(100, s))
        comment = str(sc.get("comment") or "").strip()
        against = str(sc.get("against") or "").strip()
        # 第 2 次调用（较高温 0.7）：基于已定分数生成自然点评（点评活泼不呆板）
        # 失败不影响分数，仅降级用第一次的 comment。
        try:
            polish_prompt = [
                {"role": "system", "content": (
                    "你是友善的学习辅导老师。根据【学生作答】与【参考答案】，就刚才得到的分数 "
                    f"（{s} 分）写 1-3 句中文点评：先肯定优点，再点出 1-2 个具体可改进点，语气亲切具体。"
                    "只返回 JSON：{comment: 点评}" 
                )},
                {"role": "user", "content": f"题目与答案：\n{payload}\n学生已得 {s} 分。"},
            ]
            raw2 = call_ai(polish_prompt, max_tokens=200, tier="heavy", retries=1,
                           temperature=0.7, route="score")
            polished = validate_object(raw2, {"comment": {"type": "string", "required": True}})
            c2 = str(polished.get("comment") or "").strip()
            if c2:
                comment = c2
        except Exception:
            pass  # 点评增强失败 → 保留第一次的 comment
        return {"score": s, "comment": comment, "against": against}
    except (SchemaError, ValueError) as exc:
        LOG.warning("AI 评分校验失败，降级自评: %s", exc)
    except Exception as exc:
        LOG.warning("AI 评分失败，降级自评: %s", exc)
    return {"score": None, "comment": "", "against": ""}
