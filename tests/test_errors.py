"""测试 A3 错因枚举与归并。"""
from __future__ import annotations

import unittest

from errors import ERROR_TYPES, is_valid_error_type, normalize_error_type


class TestErrorTypes(unittest.TestCase):
    def test_enum_members(self):
        self.assertEqual(len(ERROR_TYPES), 7)
        self.assertIn("concept_misunderstood", ERROR_TYPES)
        self.assertIn("heuristic_trap", ERROR_TYPES)

    def test_valid_passthrough(self):
        for et in ERROR_TYPES:
            self.assertEqual(normalize_error_type(et), et)

    def test_legacy_merge(self):
        self.assertEqual(normalize_error_type("概念不清"), "concept_misunderstood")
        self.assertEqual(normalize_error_type("算错"), "calculation")
        self.assertEqual(normalize_error_type("马虎"), "careless")
        self.assertEqual(normalize_error_type("时间不够"), "time_pressure")

    def test_frontend_option_labels_merge(self):
        """F2 守护：编辑弹窗所有错因选项（含中文旧标签）都能归一化到枚举，不丢数据。"""
        options = {
            "待诊断": "待诊断",
            "概念错误": "concept_misunderstood",
            "概念理解": "concept_misunderstood",
            "建模错误": "concept_misunderstood",
            "计算错误": "calculation",
            "粗心笔误": "careless",
            "符号/方向": "careless",
            "符号方向": "careless",
            "适用条件": "待诊断",  # 前端不再提供此选项，保留兜底行为
            "其他": "待诊断",
            "审题错误": "misread",
            "公式/事实空白": "blank_in_facts",
            "直觉陷阱": "heuristic_trap",
            "时间压力": "time_pressure",
        }
        for label, expected in options.items():
            self.assertEqual(normalize_error_type(label), expected, label)

    def test_unknown_becomes_undiagnosed(self):
        self.assertEqual(normalize_error_type("完全不知道"), "待诊断")
        self.assertEqual(normalize_error_type(""), "待诊断")
        self.assertEqual(normalize_error_type(None), "待诊断")

    def test_is_valid(self):
        self.assertTrue(is_valid_error_type("careless"))
        self.assertTrue(is_valid_error_type("待诊断"))
        self.assertFalse(is_valid_error_type("随便写"))


if __name__ == "__main__":
    unittest.main()
