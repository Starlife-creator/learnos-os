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


if __name__ == "__main__":
    unittest.main()
