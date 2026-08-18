"""P4a 备份完整性校验测试：导出含 sha256、篡改抛错、旧备份（无 sha256）兼容。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hashlib

import config
import db
import backup
from db import now


class TestBackupIntegrity(unittest.TestCase):
    # 在独立临时库上验证，避免污染共享测试 DB
    @classmethod
    def setUpClass(cls):
        cls._saved = {
            "config.DB_PATH": config.DB_PATH,
            "db.DB_PATH": db.DB_PATH,
            "config.APP_DIR": config.APP_DIR,
            "backup.APP_DIR": backup.APP_DIR,
        }

    @classmethod
    def tearDownClass(cls):
        config.DB_PATH = cls._saved["config.DB_PATH"]
        db.DB_PATH = cls._saved["db.DB_PATH"]
        config.APP_DIR = cls._saved["config.APP_DIR"]
        backup.APP_DIR = cls._saved["backup.APP_DIR"]

    def _isolated(self) -> str:
        d = tempfile.mkdtemp()
        config.DB_PATH = Path(d) / "t.db"
        db.DB_PATH = config.DB_PATH
        config.APP_DIR = Path(d) / "app"
        config.APP_DIR.mkdir(parents=True, exist_ok=True)
        backup.APP_DIR = config.APP_DIR
        db.init_db()
        with db.db() as conn:
            conn.execute(
                "INSERT INTO problems(title, course, topic, content, mastery, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("备份题", "光学", "折射", "内容", 3, now(), now()),
            )
        return d

    def test_export_includes_sha256(self):
        self._isolated()
        data = backup.export_backup()
        self.assertIn("sha256", data)
        self.assertEqual(len(data["sha256"]), 64)
        # 导出侧与还原校验侧必须同源：用相同规范化序列化重算应等于导出值
        recomputed = hashlib.sha256(
            json.dumps(data["tables"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.assertEqual(recomputed, data["sha256"])

    def test_tampered_backup_raises(self):
        self._isolated()
        data = backup.export_backup()
        # 篡改表数据但不更新 sha256（模拟损坏/篡改）
        data["tables"]["problems"][0]["title"] = "被篡改的内容"
        raw = json.dumps(data)
        with self.assertRaises(ValueError):
            backup.restore_backup(raw)

    def test_legacy_backup_without_sha256_restores(self):
        self._isolated()
        # 旧格式（无 sha256 字段）应可还原且不抛错（仅警告）
        payload = {"version": 1, "exported_at": now(), "tables": {}}
        result = backup.restore_backup(json.dumps(payload))
        self.assertIn("restored", result)


if __name__ == "__main__":
    unittest.main()
