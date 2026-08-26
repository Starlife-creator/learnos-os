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


def _repair_truncated(s: str) -> str:
    """修复 max_tokens 截断导致的"字符串未闭合"：删掉末尾不完整的字符串片段。

    模型输出到达 max_tokens 上限时 JSON 会在字符串中间被切断，形如
    {"concepts": [{"name": "惯性", "chapter": "力 —— 末尾引号缺失。
    策略：找到最后一个引号（属于未闭合字符串的起始引号），截掉其残余部分，
    若截断处缺值则补 ""，按括号配平补上缺失的 ] } 闭合符，然后尝试解析。
    修复失败返回原串。
    """
    if '"' not in s:
        # 无引号：只补括号闭合
        stripped = s.rstrip()
        stack: list[str] = []
        for ch in stripped:
            if ch in "[{":
                stack.append(ch)
            elif ch in "]}":
                if stack:
                    stack.pop()
        pairs = {"{": "}", "[": "]"}
        final = (stripped + "".join(pairs[c] for c in reversed(stack))).rstrip()
        try:
            json.loads(final)
            return final
        except (json.JSONDecodeError, TypeError):
            return s
    stripped = s.rstrip()
    # 判断是否有"未闭合字符串"：解析到末尾时字符串计数是否非零（用简易扫描）
    # 若末尾落在字符串内部（引号后无匹配闭合），则属于字符串截断 → 定位最后一个开引号
    has_unclosed_str = False
    in_str = False
    esc = False
    for ch in stripped:
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
    if in_str:
        has_unclosed_str = True
    # 从右往左找最后一个 '"'（属于未闭合字符串的起始引号），跳过转义
    cut = -1
    i = len(stripped) - 1
    while i >= 0:
        if stripped[i] == '"':
            j = i
            backslashes = 0
            while j > 0 and stripped[j - 1] == "\\":
                backslashes += 1
                j -= 1
            if backslashes % 2 == 0:
                cut = i
                break
        i -= 1
    if has_unclosed_str and cut >= 0:
        base = stripped[:cut].rstrip()
    else:
        # 无未闭合字符串：内容完整只缺括号 → 直接补括号
        base = stripped
    # 缺值场景：base 以 ':' 或 ',' 或 '[' 结尾 → 该处值/元素被截断，补一个空字符串
    # （注意：必须在这里补，否则 json.loads 在","后缺值报错）
    if base.rstrip().endswith((":", ",")):
        base = base.rstrip() + '""'
    elif base.rstrip().endswith("["):
        base = base.rstrip() + '""'
    # 括号配平补 ] }
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in base:
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
            stack.append(ch)
        elif ch in "]}":
            if stack:
                stack.pop()
    pairs = {"{": "}", "[": "]"}
    final = (base + "".join(pairs[c] for c in reversed(stack))).rstrip()
    try:
        json.loads(final)
        return final
    except (json.JSONDecodeError, TypeError):
        return s


def validate_object(raw: str, schema: dict[str, Any]) -> dict[str, Any]:
    """解析并校验 JSON 字符串。失败抛 SchemaError（不落库）。"""
    cleaned = _extract_json(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        # max_tokens 截断（字符串未闭合）→ 尝试修复后重解析
        repaired = _repair_truncated(cleaned)
        if repaired != cleaned:
            try:
                data = json.loads(repaired)
            except json.JSONDecodeError:
                raise SchemaError(f"JSON 解析失败: {exc}") from exc
        else:
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
            # C9 约定：长度规则仅约束字符串；非字符串值由 type 规则兜底，此处不做隐式 str 化
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
