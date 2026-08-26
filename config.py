"""全局配置与路径常量。"""
from __future__ import annotations

import logging
import os
import re
import secrets
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# ── 日志器（必须先于其它使用，避免 NameError）──
LOG = logging.getLogger("learnos")

BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
STATIC_DIR = BUNDLE_ROOT / "static"
DB_PATH = Path(os.environ.get("LEARNOS_DB", APP_DIR / "learnos.db"))

# ── 网络配置（安全矩阵见 README「安全部署」）──
# 暴露控制三要素，职责互不重叠：
#   LEARNOS_HOST           控制监听地址（127.0.0.1 回环 / 0.0.0.0·局域网 IP 暴露）
#   LEARNOS_ALLOW_LAN      仅抑制"暴露但未显式放行"的启动警告，不改变任何鉴权行为
#   LEARNOS_API_TOKEN      暴露模式下的写操作 Bearer 令牌；暴露且缺失 → 拒绝启动（R2）
HOST = os.environ.get("LEARNOS_HOST", "127.0.0.1")
try:
    PORT = int(os.environ.get("LEARNOS_PORT", "8765"))
except ValueError:
    LOG.warning("LEARNOS_PORT 值无效，使用默认端口 8765")
    PORT = 8765

# 一次性本地导出令牌（§1.1/§16.6）：导出整库/错题库必须携带，
# 防止同机恶意网页跨源 fetch 整库。启动即生成，不落盘、不进日志。
# R1/R5：不再随 /api/bootstrap 回显，仅作 HMAC 签名密钥（challenge 用后即焚）；
# 可用 LEARNOS_EXPORT_TOKEN 固定以便跨重启稳定（否则每次启动换新）。
EXPORT_TOKEN = os.environ.get("LEARNOS_EXPORT_TOKEN", "") or secrets.token_hex(16)
ALLOW_LAN = os.environ.get("LEARNOS_ALLOW_LAN", "") == "1"

# 暴露态写鉴权令牌（R2）：当 HOST 非回环（服务器对网络开放）时，
# 所有写/删/还原/导出操作必须携带 `Authorization: Bearer <LEARNOS_API_TOKEN>`。
# 缺省为空 → 暴露模式启动即拒绝（杜绝「静默回退无认证」这一头号雷）。
API_TOKEN = os.environ.get("LEARNOS_API_TOKEN", "").strip()

# ── AI 配置（支持环境变量，优先级高于本地数据库）──
API_KEY_ENV = os.environ.get("LEARNOS_API_KEY", "").strip()
API_BASE_ENV = os.environ.get("LEARNOS_API_BASE", "").strip()
MODEL_ENV = os.environ.get("LEARNOS_MODEL", "").strip()

LOG_FILE = APP_DIR / "learnos.log"
MEDIA_DIR = APP_DIR / "media"


class SecretRedactor(logging.Filter):
    """脱敏过滤器：遮蔽日志中的 API Key 等敏感片段。"""

    _PATTERNS = [
        re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
        re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]{8,}"),
        re.compile(r"((?:api_)?key['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9_\-]{8,}"),
        re.compile(r"(token['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9_\-]{8,}"),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.getMessage())
        for pat in self._PATTERNS:
            msg = pat.sub(lambda m: m.group(1) + "***REDACTED***" if m.groups() else "***REDACTED***", msg)
        record.msg = msg
        record.args = ()
        return True


def setup_logging() -> None:
    """配置控制台 + 滚动文件日志，并统一脱敏。幂等。"""
    if LOG.handlers:
        return
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    # Windows 控制台默认 GBK，强制 UTF-8 避免中文日志乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    redactor = SecretRedactor()

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.addFilter(redactor)
    LOG.addHandler(ch)

    try:
        fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(redactor)
        LOG.addHandler(fh)
    except OSError:
        # 文件不可写时不影响控制台日志
        pass


setup_logging()

SCHEMA = """
CREATE TABLE IF NOT EXISTS problems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    course TEXT NOT NULL DEFAULT '',
    topic TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    my_attempt TEXT NOT NULL DEFAULT '',
    error_type TEXT NOT NULL DEFAULT '待诊断',
    mastery INTEGER NOT NULL DEFAULT 1,
    ease_factor REAL NOT NULL DEFAULT 2.5,
    repetition INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    starred INTEGER NOT NULL DEFAULT 0,
    error_path TEXT NOT NULL DEFAULT '',
    trap_note TEXT NOT NULL DEFAULT '',
    shortcut TEXT NOT NULL DEFAULT '',
    fix_action TEXT NOT NULL DEFAULT '',
    state INTEGER NOT NULL DEFAULT 0,
    stability REAL NOT NULL DEFAULT 0.0,
    difficulty REAL NOT NULL DEFAULT 0.0,
    tags TEXT NOT NULL DEFAULT '[]',
    tags_suggested TEXT NOT NULL DEFAULT '[]',
    tags_status TEXT NOT NULL DEFAULT '',
    variants TEXT NOT NULL DEFAULT '[]',
    concept_ids TEXT NOT NULL DEFAULT '[]',
    media_path TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS hints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id INTEGER NOT NULL,
    level INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(problem_id) REFERENCES problems(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id INTEGER NOT NULL,
    due_date TEXT NOT NULL,
    interval_days INTEGER NOT NULL DEFAULT 1,
    completed INTEGER NOT NULL DEFAULT 0,
    result TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(problem_id) REFERENCES problems(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS oral_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    transcript TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""

# D1（R4 合规）：密钥一律不落库。仅保存非敏感配置。
#
# 配置单一真相源（§16.3）：SETTINGS_SCHEMA 同时驱动默认值与写白名单，
# 消除 handler.py 中手写 allowed 集合与重复 coercion 逻辑。
# type 支持：str / int / float / bool / subject。
# subject 类型需要注册表校验（避免 config 反向依赖 db），由调用方传入 valid_subject_fn。
SETTINGS_SCHEMA: dict[str, dict[str, Any]] = {
    "api_base":         {"type": "str",   "default": "https://api.openai.com/v1"},
    "model":            {"type": "str",   "default": ""},
    "temperature":      {"type": "float", "min": 0.0, "max": 2.0, "default": "0.3"},
    "fast_model":       {"type": "str",   "default": ""},
    "heavy_model":      {"type": "str",   "default": ""},
    "vision_model":     {"type": "str",   "default": ""},
    # M8 可选向量检索：填写则启用（复用同一 AI 端点的 /v1/embeddings，零第三方依赖）；
    # 留空则 RAG 检索保持纯 BM25，向量化链路整体降级。
    "embedding_model":  {"type": "str",   "default": ""},
    "default_subject":  {"type": "subject", "default": "physics"},
    "hint_cache_enabled": {"type": "bool", "default": "1"},
    "daily_review_cap": {"type": "int",   "min": 0,   "max": 500, "default": "0"},
    "ai_context_tokens": {"type": "int",  "min": 4000, "max": 1_000_000, "default": "32000"},
    "allow_local_ai":   {"type": "bool",  "default": "1"},
    # 关闭 reasoner 思考模式（enable_thinking=false）：阿里云百炼等 DeepSeek 兼容端点，
    # 非流式请求必须关闭思考，否则报错/仅返回推理内容。默认关闭以适配 JSON 提取等结构化任务。
    "disable_thinking": {"type": "bool",  "default": "1"},
    # 模型单次输出 token 上限（自动约束各调用点 max_tokens，防越界/防截断）
    "max_output_tokens": {"type": "int",  "min": 512, "max": 32768, "default": "4096"},
    # M1 提示词缓存断点（可选，默认关）：仅 Anthropic 原生/兼容代理需要显式 cache_control；
    # DeepSeek/OpenAI 系对稳定前缀自动缓存，无需开启。端点拒绝该字段时由 call_ai 的
    # 400 剥离重试自动降级（去掉断点重发一次）。
    "prompt_cache_control": {"type": "bool", "default": "0"},
    # M6 结构化输出强制（可选，默认关）：对 JSON 产出的调用点下发
    # response_format={"type":"json_object"}，把「只返回 JSON」从提示词约束升级为协议约束。
    # 本地/严格 OpenAI 兼容端点不支持时由 400 剥离重试降级回纯提示词约束 + validate_object。
    "json_response_format": {"type": "bool", "default": "0"},
}


def coerce_setting(key: str, raw_value: Any, valid_subject_fn=None) -> str:
    """按 SETTINGS_SCHEMA 校验并转换可落库的字符串值。

    - bool：空/0/false/off → "0"，其余 → "1"
    - int/float：越界 clamp 到 [min, max]；空/非法回退 schema 默认值
    - subject：传 valid_subject_fn 时经其归一化，否则原样（空回退默认）
    - str：去空格原样
    非法/未知 key 抛 ValueError（调用方捕获为 400）。
    """
    spec = SETTINGS_SCHEMA.get(key)
    if spec is None:
        raise ValueError(f"未知设置项: {key}")
    raw = "" if raw_value is None else str(raw_value).strip()
    t = spec["type"]
    if t == "bool":
        return "0" if raw in ("", "0", "false", "off", "False") else "1"
    if t == "int":
        try:
            v = int(raw or spec["default"])
        except (TypeError, ValueError):
            v = int(spec["default"])
        return str(max(spec.get("min", v), min(spec.get("max", v), v)))
    if t == "float":
        try:
            v = float(raw or spec["default"])
        except (TypeError, ValueError):
            v = float(spec["default"])
        return str(max(spec.get("min", v), min(spec.get("max", v), v)))
    if t == "subject":
        if valid_subject_fn is not None and raw:
            return valid_subject_fn(raw)
        return raw or spec["default"]
    return raw


# 由 schema 自动派生默认值（单一真相源）：新增设置项只需改 SETTINGS_SCHEMA。
DEFAULT_SETTINGS = {k: v["default"] for k, v in SETTINGS_SCHEMA.items()}
