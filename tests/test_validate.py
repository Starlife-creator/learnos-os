"""测试 C4 结构化校验器。"""
from __future__ import annotations

import unittest

from validate import SchemaError, validate_object

SCHEMA = {
    "error_type": {"type": "string", "enum": ["calculation", "careless"], "required": True},
    "confidence": {"type": "number", "min": 0.0, "max": 1.0},
    "tags": {"type": "array", "items": {"type": "string"}, "required": True},
    "meta": {"type": "object", "properties": {"source": {"type": "string"}}},
}


class TestValidate(unittest.TestCase):
    def test_valid_object(self):
        data = validate_object(
            '{"error_type": "careless", "confidence": 0.9, "tags": ["力学"], "meta": {"source": "ai"}}',
            SCHEMA,
        )
        self.assertEqual(data["error_type"], "careless")

    def test_missing_required(self):
        with self.assertRaises(SchemaError):
            validate_object('{"confidence": 0.5}', SCHEMA)

    def test_invalid_enum(self):
        with self.assertRaises(SchemaError):
            validate_object('{"error_type": "bogus", "tags": []}', SCHEMA)

    def test_type_mismatch(self):
        with self.assertRaises(SchemaError):
            validate_object('{"error_type": "careless", "tags": "not-array"}', SCHEMA)

    def test_out_of_range(self):
        with self.assertRaises(SchemaError):
            validate_object('{"error_type": "careless", "confidence": 1.5, "tags": []}', SCHEMA)

    def test_bad_json(self):
        with self.assertRaises(SchemaError):
            validate_object("{not json", SCHEMA)

    def test_unknown_keys_ignored(self):
        data = validate_object('{"error_type": "calculation", "tags": [], "extra": 1}', SCHEMA)
        self.assertEqual(data["extra"], 1)

    def test_nested_object_validation(self):
        with self.assertRaises(SchemaError):
            validate_object('{"error_type": "calculation", "tags": [], "meta": {"source": 42}}', SCHEMA)

    def test_fence_codeblock_stripped(self):
        data = validate_object(
            '```json\n{"error_type": "careless", "tags": ["力学"]}\n```',
            SCHEMA,
        )
        self.assertEqual(data["error_type"], "careless")

    def test_prefix_prose_stripped(self):
        data = validate_object(
            '以下是分析结果：\n{"error_type": "calculation", "tags": ["代数"]}\n希望有帮助',
            SCHEMA,
        )
        self.assertEqual(data["error_type"], "calculation")
        self.assertEqual(data["tags"], ["代数"])

    def test_markdown_fence_plain(self):
        data = validate_object(
            '```\n{"error_type": "careless", "confidence": 0.8, "tags": []}\n```',
            SCHEMA,
        )
        self.assertEqual(data["confidence"], 0.8)

    def test_pure_prose_still_fails(self):
        # 无任何 JSON 结构的纯文本仍应报错（不误吞）
        with self.assertRaises(SchemaError):
            validate_object("这道题考查受力分析，建议多练习", SCHEMA)

    def test_truncated_string_repaired(self):
        # max_tokens 截断（字符串未闭合）→ 自动修复：丢弃截断残余并补闭合
        data = validate_object(
            '{"error_type": "calculation", "tags": ["代"',
            SCHEMA,
        )
        self.assertEqual(data["error_type"], "calculation")
        self.assertTrue(data["tags"] == ["代"] or data["tags"] == [])

    def test_truncated_bracket_closed(self):
        # 数组截断（元素写到一半）→ 补空值+闭合括号
        data = validate_object(
            '{"error_type": "careless", "tags": ["力学", "',
            SCHEMA,
        )
        self.assertEqual(data["error_type"], "careless")
        self.assertEqual(data["tags"], ["力学", ""])


if __name__ == "__main__":
    unittest.main()
