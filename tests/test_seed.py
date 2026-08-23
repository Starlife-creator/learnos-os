#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核心种子/连接函数的直接单测（固化 CI 不回归）。

覆盖今天安全审计引入的关键函数：
- db.normalize_subject        : 学科 id 大小写/空格归一，防双副本空壳科
- scripts.apply_seed_content  : 详解落库前的 missing/empty 校验（防详解找不到库概念或空串写库）
- db.close_all_connections    : 整库还原前关闭所有线程连接 + 递增 epoch，确保 db() 能重建
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config          # noqa: E402
import db              # noqa: E402
import graph           # noqa: E402
from db import normalize_subject, close_all_connections  # noqa: E402
import scripts.apply_seed_content as asc  # noqa: E402

_TEST_TMP_DIR = os.path.join(ROOT, "tests", ".tmp")


class TestNormalizeSubject(unittest.TestCase):
    """normalize_subject 是学科 id 归一的单一哨兵点，必须稳。"""

    def test_lowercase(self):
        self.assertEqual(normalize_subject("Music"), "music")
        self.assertEqual(normalize_subject("BIOLOGY"), "biology")

    def test_strip_whitespace(self):
        self.assertEqual(normalize_subject("  Bio "), "bio")
        self.assertEqual(normalize_subject("\tCHEM\n"), "chem")

    def test_empty_and_none(self):
        self.assertEqual(normalize_subject(""), "")
        # None 被 or "" 吸收为 ""（falsy 短路），归一后仍为 ""
        self.assertEqual(normalize_subject(None), "")

    def test_subject_exists_uses_normalize(self):
        # 大小写变体查询同一学科，不产生独立大写壳
        self.assertTrue(db.subject_exists("biology"))
        self.assertTrue(db.subject_exists("Biology"))
        self.assertTrue(db.subject_exists("BIOLOGY"))


class TestApplySeedContent(unittest.TestCase):
    """apply_subject 的校验逻辑是落库前的最后防线。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="seed_", dir=_TEST_TMP_DIR)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "seed_test.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()
        # 加载一个真实学科到临时库，使其含概念节点
        graph.ensure_seed("biology")

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db
        cls._tmp.cleanup()

    def test_load_explanations_real_file(self):
        # biology 的详解文件已进 git，应非空
        expl = asc._load_explanations("biology")
        self.assertIsInstance(expl, dict)
        self.assertGreater(len(expl), 0)
        # 所有详解值应为非空字符串
        for name, text in expl.items():
            self.assertIsInstance(text, str)
            self.assertTrue(text.strip(), f"空详解: {name}")

    def test_apply_commit_writes_explanation(self):
        # 临时库加载 biology 后，概念应无详解
        with db.db() as conn:
            before = conn.execute(
                "SELECT COUNT(*) c FROM concepts WHERE subject='biology' AND explanation<>''"
            ).fetchone()["c"]
        self.assertEqual(before, 0)
        rc = asc.apply_subject("biology", commit=True)
        self.assertEqual(rc, 0)
        with db.db() as conn:
            after = conn.execute(
                "SELECT COUNT(*) c FROM concepts WHERE subject='biology' AND explanation<>''"
            ).fetchone()["c"]
        self.assertEqual(after, len(asc._load_explanations("biology")))

    def test_apply_dry_run_does_not_write(self):
        # 新学科 history 仅校验不写库
        graph.ensure_seed("history")
        with db.db() as conn:
            before = conn.execute(
                "SELECT COUNT(*) c FROM concepts WHERE subject='history' AND explanation<>''"
            ).fetchone()["c"]
        self.assertEqual(before, 0)
        rc = asc.apply_subject("history", commit=False)
        self.assertEqual(rc, 0)
        with db.db() as conn:
            after = conn.execute(
                "SELECT COUNT(*) c FROM concepts WHERE subject='history' AND explanation<>''"
            ).fetchone()["c"]
        self.assertEqual(after, 0)


class TestCloseAllConnections(unittest.TestCase):
    """close_all_connections 是 R4 连接复用的关键：还原前释放文件锁 + 递增 epoch。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="conn_", dir=_TEST_TMP_DIR)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / "conn_test.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db
        cls._tmp.cleanup()

    def test_close_then_reopen(self):
        # 先打开一个连接（进 _ALL_CONNS），再关闭，再 db() 应能重建且不抛错
        with db.db() as conn:
            conn.execute("SELECT 1")
        # 记录 epoch 前
        epoch_before = db._EPOCH
        close_all_connections()
        self.assertEqual(db._EPOCH, epoch_before + 1)
        # 关闭后 db() 必须能重新建立连接并查询
        with db.db() as conn:
            self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)

    def test_idempotent(self):
        close_all_connections()
        close_all_connections()
        with db.db() as conn:
            self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
