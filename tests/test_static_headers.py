"""P5 静态资源缓存头测试：vendor 长缓存 / 业务资源 no-cache。

说明：P5 本轮仅落地 Cache-Control（零风险、真实收益）；gzip 因 localhost 收益极低、
且重写 send_head 风险高，按"收益最大化风险最小化"原则显式推迟（见优化方案 ADR），
对应断言见 test_gzip_deferred。
"""
import http.client
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
from handler import Handler

_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)


class TestStaticHeaders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved_db = config.DB_PATH
        cls._saved_app = config.APP_DIR
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="static_", dir=_TMP)
        config.DB_PATH = Path(cls.temp_dir.name) / "t.db"
        db.DB_PATH = config.DB_PATH
        # APP_DIR 不影响静态服务（STATIC_DIR 为仓库真实静态目录），仅占位避免副作用
        config.APP_DIR = Path(cls.temp_dir.name) / "app"
        config.APP_DIR.mkdir(parents=True, exist_ok=True)
        db.init_db()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        config.DB_PATH = cls._saved_db
        config.APP_DIR = cls._saved_app
        cls.temp_dir.cleanup()

    def _get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        body = resp.read()
        conn.close()
        return resp.status, hdrs, body

    def test_root_gets_no_cache(self):
        # 业务资源（index.html）应得 no-cache
        status, hdrs, _ = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("cache-control", hdrs)
        self.assertIn("no-cache", hdrs["cache-control"])

    def test_vendor_path_gets_immutable(self):
        # 即便资源不存在（404），路径含 /vendor/ 也应得 immutable 长缓存头
        status, hdrs, _ = self._get("/vendor/does-not-exist.js")
        self.assertIn("cache-control", hdrs)
        self.assertIn("immutable", hdrs["cache-control"])
        self.assertIn("max-age=31536000", hdrs["cache-control"])

    def test_gzip_deferred(self):
        # 当前 P5 仅落地 Cache-Control；gzip 显式推迟（见优化方案 ADR）。
        # 断言：声明 Accept-Encoding: gzip 时文本资源仍正常返回，且不发 Content-Encoding: gzip。
        status, hdrs, _ = self._get("/", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertNotIn("content-encoding", hdrs)


if __name__ == "__main__":
    unittest.main()
