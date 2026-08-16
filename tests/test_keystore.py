"""测试 D1 密钥存储（keys.enc 加密文件 + 内存降级）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import config
import keystore
from config import APP_DIR

_TMP = Path(tempfile.gettempdir()) / "learnos_tests" / "keystore"


class TestKeystore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        keystore.KEY_FILE = _TMP / "keys.enc"
        keystore.KEY_FILE.parent.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        keystore.clear_key()

    def test_clear(self):
        self.assertFalse(keystore.key_file_exists())

    def test_save_load_roundtrip(self):
        if not keystore.crypto_available():
            self.skipTest("cryptography 不可用，降级为内存密钥")
        self.assertTrue(keystore.save_key("sk-test-12345", "p@ssw0rd"))
        self.assertTrue(keystore.key_file_exists())
        self.assertEqual(keystore.load_key("p@ssw0rd"), "sk-test-12345")

    def test_wrong_password(self):
        if not keystore.crypto_available():
            self.skipTest("cryptography 不可用，降级为内存密钥")
        keystore.save_key("sk-test-12345", "correct")
        self.assertIsNone(keystore.load_key("wrong"))

    def test_no_password_save_fails(self):
        self.assertFalse(keystore.save_key("sk-x", ""))
        self.assertFalse(keystore.key_file_exists())

    def test_plaintext_never_on_disk(self):
        if not keystore.crypto_available():
            self.skipTest("cryptography 不可用，降级为内存密钥")
        keystore.save_key("sk-super-secret-value", "pw")
        raw = keystore.KEY_FILE.read_text(encoding="utf-8")
        self.assertNotIn("sk-super-secret-value", raw)

    def test_empty_key_save_fails(self):
        self.assertFalse(keystore.save_key("", "pw"))

    def test_load_missing_file(self):
        self.assertIsNone(keystore.load_key("pw"))


if __name__ == "__main__":
    unittest.main()
