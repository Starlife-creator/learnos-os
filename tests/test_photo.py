"""B1 拍照/截图录题测试：上传、附件存取、视觉降级（R3）、防穿越。"""
import base64
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
from handler import Handler

_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)

# 1x1 红色 PNG
_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
            "hKmMIQAAAABJRU5ErkJggg==")


class TestPhotoUpload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="photo_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls.temp_dir.name) / "photo_test.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()
        cls._orig_media = config.MEDIA_DIR
        config.MEDIA_DIR = Path(cls.temp_dir.name) / "media"
        config.MEDIA_DIR.mkdir(exist_ok=True)
        import handler as handler_mod
        cls._handler_mod = handler_mod
        cls._orig_handler_media = handler_mod.MEDIA_DIR
        handler_mod.MEDIA_DIR = config.MEDIA_DIR  # 值拷贝，需同步替换
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        config.MEDIA_DIR = cls._orig_media
        cls._handler_mod.MEDIA_DIR = cls._orig_handler_media
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db
        cls.temp_dir.cleanup()

    def request(self, path, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "X-Requested-With": "PhysicsStudyOS"},
        )
        with urllib.request.urlopen(req, timeout=8) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def request_error(self, path, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "X-Requested-With": "PhysicsStudyOS"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=8)
        err = ctx.exception
        return err.code, json.loads(err.read().decode("utf-8"))

    def test_upload_valid_png(self):
        status, r = self.request("/api/upload/photo", "POST", {"data": _PNG_B64, "name": "a.png"})
        self.assertEqual(status, 200)
        self.assertTrue(r["path"].startswith("media/"))
        self.assertTrue(r["path"].endswith(".png"))
        self.assertTrue((config.MEDIA_DIR / Path(r["path"]).name).is_file())

    def test_upload_rejects_non_image(self):
        code, r = self.request_error("/api/upload/photo", "POST", {"data": "aGVsbG8="})
        self.assertEqual(code, 400)
        self.assertIn("PNG/JPEG", r["error"])

    def test_upload_rejects_bad_base64(self):
        code, r = self.request_error("/api/upload/photo", "POST", {"data": "!!!notbase64!!!"})
        self.assertEqual(code, 400)

    def test_media_serving(self):
        status, r = self.request("/api/upload/photo", "POST", {"data": _PNG_B64})
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/{r['path']}", timeout=8) as resp:
            body = resp.read()
        self.assertEqual(body, base64.b64decode(_PNG_B64))

    def test_media_traversal_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/media/..%2F..%2Fphysics_study.db", timeout=8)
        self.assertEqual(ctx.exception.code, 400)

    def test_extract_photo_degrades_without_vision(self):
        status, r = self.request("/api/upload/photo", "POST", {"data": _PNG_B64})
        status2, r2 = self.request("/api/ai/extract-photo", "POST", {"media_path": r["path"]})
        self.assertEqual(status2, 200)
        self.assertIsNone(r2["draft"])
        self.assertTrue(r2["degraded"])

    def test_extract_photo_missing_file(self):
        code, r = self.request_error("/api/ai/extract-photo", "POST", {"media_path": "media/nope.png"})
        self.assertEqual(code, 400)

    def test_create_problem_with_media(self):
        status, r = self.request("/api/upload/photo", "POST", {"data": _PNG_B64})
        rel = r["path"]
        status, created = self.request("/api/problems", "POST", {
            "title": "带图题目", "topic": "电磁感应", "course": "大学物理",
            "content": "看图作答", "media_path": rel,
        })
        self.assertEqual(status, 201)
        status, detail = self.request(f"/api/problems/{created['id']}")
        self.assertIn(rel, detail["media_list"])
        self.assertIn(rel, detail["media_path"])

    def test_create_problem_rejects_external_path(self):
        status, created = self.request("/api/problems", "POST", {
            "title": "越界图", "topic": "x", "course": "x",
            "content": "x", "media_path": "../outside.png",
        })
        self.assertEqual(status, 201)
        _, detail = self.request(f"/api/problems/{created['id']}")
        self.assertEqual(detail["media_list"], [])


if __name__ == "__main__":
    unittest.main()
