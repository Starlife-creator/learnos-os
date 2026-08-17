"""结构化输出校验（C4）：自研轻量校验器，零第三方依赖。

支持：必填字段、类型、枚举约束、数值区间、嵌套 dict/list。
AI 返回的 JSON 必须先过此校验才能落库（R3）。

解析时对 AI 常见不洁输出做容错：
- Markdown 代码块包裹（```json ... ```）→ 剥离
- 前置说明文字（"以下是结果："）→ 从首个 {/[ 处截取
- 后置多余文字 → 截断到 JSON 结束
"""

from __future__ import annotations

import json
import re
from typing import Any


class SchemaError(ValueError):
    """校验失败，携带可读的字段路径信息。"""


def _extract_json(raw: str) -> str:
    """从 AI 回复中提取 JSON 主体：剥离代码块、截取首个 {/[ 到末尾对应的 }/]。

    解析失败返回原串（交由 json.loads 给出原始错误）。
    """
    s = str(raw).strip()
    if not s:
        return s
    # 剥离 Markdown 代码块围栏（```json ... ``` 或 ``` ... ```）
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    # 前置说明文字：从首个 { 或 [ 开始
    start = min([i for i in (s.find("{"), s.find("[")) if i >= 0] or [-1])
    if start > 0:
        s = s[start:]
    # 后置多余文字：从锚点起用括号配平找 JSON 结尾
    if s:
        s = s.rstrip()
        depth = 0
        in_str = False
        esc = False
        end = None
        for i, ch in enumerate(s):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end is not None:
            s = s[:end]
    return s


def validate_object(raw: str, schema: dict[str, Any]) -> dict[str, Any]:
    """解析并校验 JSON 字符串。失败抛 SchemaError（不落库）。"""
    cleaned = _extract_json(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"JSON 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise SchemaError(f"期望 JSON 对象，得到 {type(data).__name__}")
    _check(data, schema, "$")
    return data


def _fail(path: str, msg: str) -> None:
    raise SchemaError(f"{path}: {msg}")


def _check(data: Any, schema: dict[str, Any], path: str) -> None:
    """单层校验：schema 的每个键按规则检查。未知键忽略（宽松）。"""
    for key, rule in schema.items():
        field_path = f"{path}.{key}"
        required = bool(rule.get("required"))
        if key not in data:
            if required:
                _fail(field_path, "缺少必填字段")
            continue
        value = data[key]
        _check_value(value, rule, field_path)


def _check_value(value: Any, rule: dict[str, Any], path: str) -> None:
    for key, val in rule.items():
        if key == "required":
            continue
        if key == "type":
            ok = _type_ok(value, val)
            if not ok:
                _fail(path, f"类型应为 {val}，得到 {type(value).__name__}")
        elif key == "enum":
            if value not in val:
                _fail(path, f"值 {value!r} 不在允许枚举 {val} 中")
        elif key == "min" and not (isinstance(value, (int, float)) and value >= val):
            _fail(path, f"值 {value!r} 小于下限 {val}")
        elif key == "max" and not (isinstance(value, (int, float)) and value <= val):
            _fail(path, f"值 {value!r} 大于上限 {val}")
        elif key == "pattern":
            if not isinstance(value, str) or not re.search(val, value):
                _fail(path, f"字符串不匹配 {val}")
        elif key == "min_length":
            if not isinstance(value, str) or len(value) < val:
                _fail(path, f"字符串长度小于 {val}")
        elif key == "max_length":
            if isinstance(value, str) and len(value) > val:
                _fail(path, f"字符串长度大于 {val}")
        elif key == "items":
            if not isinstance(value, list):
                _fail(path, f"应为数组，得到 {type(value).__name__}")
            for i, item in enumerate(value):
                _check_value(item, val, f"{path}[{i}]")
        elif key == "properties":
            if not isinstance(value, dict):
                _fail(path, f"应为对象，得到 {type(value).__name__}")
            _check(value, val, path)


def _type_ok(value: Any, expect: str) -> bool:
    if expect == "string":
        return isinstance(value, str)
    if expect == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expect == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expect == "boolean":
        return isinstance(value, bool)
    if expect == "array":
        return isinstance(value, list)
    if expect == "object":
        return isinstance(value, dict)
    if expect == "any":
        return True
    return False
