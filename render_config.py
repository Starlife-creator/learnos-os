"""学科渲染配置（§29）：驱动前端多场景原语的学科感知渲染。

每个学科可覆盖：显示名、单位制、强调色、有效数字、公式渲染提示、专属图标。
存储于 settings 表的 render_config_<subject> JSON，未设置项回退到 DEFAULT_RENDER。
全部零依赖、可经 API 读写、回滚安全（仅写 settings 表）。
"""
from __future__ import annotations

import json
from typing import Any

from db import DB_LOCK, db, settings_dict

# 多场景渲染原语默认值（学科无关基线）。
DEFAULT_RENDER: dict[str, Any] = {
    "display_name": "",
    "accent": "#3b82f6",
    "units": "SI",
    "sig_figs": 3,
    "formula_engine": "plain",   # plain | latex | chemistry
    "icon": "book",
    "number_locale": "en",        # en: 1,234.5 ｜ zh: 1 234.5
    "show_steps": True,
}

# 内置三科的合理默认值（仅作首次体验，仍可被用户覆盖）。
_BUILTIN_PRESETS: dict[str, dict[str, Any]] = {
    "physics":    {"display_name": "物理", "accent": "#2563eb", "units": "SI", "sig_figs": 3, "formula_engine": "latex", "icon": "atom"},
    "chemistry":  {"display_name": "化学", "accent": "#16a34a", "units": "mol", "sig_figs": 3, "formula_engine": "chemistry", "icon": "flask"},
    "math":       {"display_name": "数学", "accent": "#9333ea", "units": "none", "sig_figs": 4, "formula_engine": "latex", "icon": "function"},
}

_KEY_PREFIX = "render_config_"


def _accept_key(k: str) -> bool:
    return k in DEFAULT_RENDER


def get_render_config(subject: str) -> dict[str, Any]:
    """合并：DEFAULT_RENDER ← 内置预设 ← 用户覆盖。"""
    subject = (subject or "").strip()
    merged = dict(DEFAULT_RENDER)
    if subject in _BUILTIN_PRESETS:
        merged.update(_BUILTIN_PRESETS[subject])
    if subject:
        stored = settings_dict().get(_KEY_PREFIX + subject, "")
        if stored:
            try:
                user = json.loads(stored)
                if isinstance(user, dict):
                    for k, v in user.items():
                        if _accept_key(k):
                            merged[k] = v
            except (json.JSONDecodeError, TypeError):
                pass
    if not merged.get("display_name") and subject:
        merged["display_name"] = subject
    return merged


def set_render_config(subject: str, patch: dict[str, Any]) -> dict[str, Any]:
    """增量更新某学科的渲染配置；仅接受白名单字段。"""
    subject = (subject or "").strip()
    if not subject:
        raise ValueError("学科标识不能为空")
    merged = get_render_config(subject)
    for k, v in (patch or {}).items():
        if _accept_key(k):
            merged[k] = v
    payload = {k: merged[k] for k in DEFAULT_RENDER}
    with DB_LOCK, db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            (_KEY_PREFIX + subject, json.dumps(payload, ensure_ascii=False)),
        )
    return merged


def all_render_configs(subjects: list[str]) -> dict[str, dict[str, Any]]:
    return {s: get_render_config(s) for s in subjects}
