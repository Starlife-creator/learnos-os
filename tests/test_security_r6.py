"""R6 验证闭环：R1-R5 安全加固的守护测试。

覆盖（对齐 6 轮方案 R6 验收项）：
- R1：/api/bootstrap 不再回显 export_token；导出需一次性 challenge；重放即拒。
- R1+R5：challenge 签名绑定客户端 IP——换 IP 重放拒。
- R2：暴露模式（HOST 非回环）无 Bearer → 写操作 401/403；回环模式不受影响。
- R3：AI 接口高频调用 → 429（ratelimit.ai_quota_ok 直接单测 + 端点级）。
- R4：_IDEMPOTENCY 并发写零异常；db() 线程本地连接复用。
- R5：破坏性操作写 audit.log。

运行注意：与全量一致，须 `PYTHONPATH= python run_tests.py`（摘掉 safe-delete shim）。
"""
import json
import os
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import ratelimit  # noqa: E402
from handler_base import _IDEMPOTENCY, _IDEMPOTENCY_LOCK, _prune_idempotency  # noqa: E402


class _TinyHandler:
    """最小 handler 替身：仅暴露 _client_ip / _export_token_ok 所需字段。"""

    def __init__(self, path="/api/export", ip="127.0.0.1", header_token=""):
        self.path = path
        self.client_address = (ip, 12345)
        self.headers = {"X-Export-Token": header_token} if header_token else {}


class TestR1BootstrapNoToken(unittest.TestCase):
    """R1：bootstrap 是静态 secret 泄漏源，必须不再回显。"""

    def test_bootstrap_response_has_no_export_token(self):
        from handler import Handler
        # 直接验证 bootstrap 响应结构（不走 HTTP，避免起服务）：无 export_token 键
        import handler
        # 读取真实响应构造逻辑：json_response 会被替换为捕获器
        captured = {}

        class _Cap:
            def json_response(self, data, status=200):
                captured["data"] = data
                captured["status"] = status

        h = Handler.__new__(Handler)
        # 只调用 _handle_bootstrap，其余属性由替身提供
        from types import SimpleNamespace
        h.json_response = _Cap().json_response
        h._handle_bootstrap()
        self.assertNotIn("export_token", captured["data"])
        self.assertEqual(captured["status"], 200)


class TestR1Challenge(unittest.TestCase):
    """R1+R5：一次性 challenge——单次有效、IP 绑定、重放拒。"""

    def test_challenge_single_use(self):
        token, _ttl = auth.issue_export_challenge(ip="127.0.0.1")
        self.assertTrue(auth.verify_export_challenge(token, ip="127.0.0.1"))
        # 第二次使用同一 token → 已消费，拒
        self.assertFalse(auth.verify_export_challenge(token, ip="127.0.0.1"))

    def test_challenge_binds_ip(self):
        token, _ttl = auth.issue_export_challenge(ip="10.0.0.5")
        # 换 IP 重放 → 签名不符，拒
        self.assertFalse(auth.verify_export_challenge(token, ip="10.0.0.6"))
        # 原 IP 仍可用（未消费）
        self.assertTrue(auth.verify_export_challenge(token, ip="10.0.0.5"))

    def test_challenge_expired_rejected(self):
        import time
        old_ttl = auth._CHALLENGE_TTL
        try:
            auth._CHALLENGE_TTL = 1
            token, _ttl = auth.issue_export_challenge(ip="127.0.0.1")
            time.sleep(1.2)
            self.assertFalse(auth.verify_export_challenge(token, ip="127.0.0.1"))
        finally:
            auth._CHALLENGE_TTL = old_ttl

    def test_challenge_tampered_rejected(self):
        token, _ttl = auth.issue_export_challenge(ip="127.0.0.1")
        nonce, exp, sig = token.split(".")
        forged = f"{nonce}.{exp}.{'0' * len(sig)}"
        self.assertFalse(auth.verify_export_challenge(forged, ip="127.0.0.1"))

    def test_challenge_guard_integration(self):
        """端点级：_guard_export_token 对有效 challenge 放行、无效拒。"""
        from handler_problems import ProblemsMixin

        class _G(ProblemsMixin, _TinyHandler):
            def json_response(self, data, status=200):
                self._status = status
                self._data = data

        # 无 token → 拒（FORBIDDEN 401）
        g = _G()
        self.assertFalse(g._guard_export_token())
        self.assertEqual(g._status, 401)
        # 有效 challenge → 放行
        token, _ = auth.issue_export_challenge(ip="127.0.0.1")
        g2 = _G(header_token=token)
        self.assertTrue(g2._guard_export_token())


class TestR2ExposedBearer(unittest.TestCase):
    """R2：暴露模式（HOST 非回环）写操作必须 Bearer；回环不受影响。"""

    def test_loopback_write_no_bearer_ok(self):
        # 回环模式（当前测试环境 HOST=127.0.0.1）：_write_auth_ok 只需 CSRF
        from handler import Handler
        import handler
        h = Handler.__new__(Handler)
        h.headers = {"X-Requested-With": "LearnOS"}
        # 默认 HOST 为回环（config.HOST），不要求 Bearer
        self.assertTrue(h._write_auth_ok())

    def test_exposed_requires_bearer(self):
        from handler import Handler
        h = Handler.__new__(Handler)
        # 模拟暴露：patch auth.is_exposed 返回 True
        orig_exposed = auth.is_exposed
        orig_token = auth.API_TOKEN  # auth.API_TOKEN 是 import 时拷贝，须 patch 模块级
        auth.is_exposed = lambda: True
        auth.API_TOKEN = "s3cret-token"
        try:
            # 有 CSRF 但无 Bearer → 拒
            h.headers = {"X-Requested-With": "LearnOS"}
            self.assertFalse(h._write_auth_ok())
            # 有 Bearer 但错误 → 拒
            h.headers = {"X-Requested-With": "LearnOS", "Authorization": "Bearer wrong"}
            self.assertFalse(h._write_auth_ok())
            # 正确 Bearer → 放行
            h.headers = {"X-Requested-With": "LearnOS", "Authorization": "Bearer s3cret-token"}
            self.assertTrue(h._write_auth_ok())
        finally:
            auth.is_exposed = orig_exposed
            auth.API_TOKEN = orig_token

    def test_app_refuses_exposed_without_token(self):
        """R2 致命缺陷守护：暴露模式且无 API_TOKEN → 启动守卫拒绝。"""
        import app
        orig_host = auth.HOST
        orig_token = app.API_TOKEN  # app.API_TOKEN 是 import 时拷贝，须 patch 模块级
        try:
            auth.HOST = "0.0.0.0"  # auth 层 HOST 是 import 时拷贝，须 patch 此处
            app.API_TOKEN = ""
            self.assertFalse(app._check_exposed_token())
        finally:
            auth.HOST = orig_host
            app.API_TOKEN = orig_token

    def test_app_starts_loopback_without_token(self):
        """回环模式无 token 必须正常（不误伤本地）。"""
        import app
        orig_host = auth.HOST
        orig_token = app.API_TOKEN
        try:
            auth.HOST = "127.0.0.1"
            app.API_TOKEN = ""
            self.assertTrue(app._check_exposed_token())
        finally:
            auth.HOST = orig_host
            app.API_TOKEN = orig_token

    def test_app_allows_exposed_with_token(self):
        """暴露模式 + 正确 token → 启动守卫放行。"""
        import app
        orig_host = auth.HOST
        orig_token = app.API_TOKEN
        try:
            auth.HOST = "0.0.0.0"
            app.API_TOKEN = "s3cret-token"
            self.assertTrue(app._check_exposed_token())
        finally:
            auth.HOST = orig_host
            app.API_TOKEN = orig_token


class TestR3AiQuota(unittest.TestCase):
    """R3：AI 调用配额——高频超限 429 语义（ratelimit 层直接验证）。"""

    def setUp(self):
        ratelimit.clear_all()

    def tearDown(self):
        ratelimit.clear_all()

    def test_fast_quota_exhausted(self):
        # 快速打满 fast 档（默认 40）→ 第 41 次拒
        ip = "10.0.0.11"
        for _ in range(40):
            self.assertTrue(ratelimit.ai_quota_ok(ip, "fast"))
        self.assertFalse(ratelimit.ai_quota_ok(ip, "fast"))

    def test_heavy_quota_exhausted(self):
        ip = "10.0.0.12"
        for _ in range(10):
            self.assertTrue(ratelimit.ai_quota_ok(ip, "heavy"))
        self.assertFalse(ratelimit.ai_quota_ok(ip, "heavy"))

    def test_loopback_relaxed(self):
        # 回环同样计费（本地高频也会触顶），但阈值可调——此处验证 fail-open 不受影响
        ip = "127.0.0.1"
        self.assertTrue(ratelimit.ai_quota_ok(ip, "heavy"))

    def test_fail_open_on_module_error(self):
        # 限流器异常不得阻塞学习：把内部表换成坏类型触发异常 → 放行
        orig = ratelimit._ai_calls
        try:
            ratelimit._ai_calls = None  # type: ignore[assignment]
            self.assertTrue(ratelimit.ai_quota_ok("10.0.0.13", "heavy"))
        finally:
            ratelimit._ai_calls = orig


class TestR4Concurrency(unittest.TestCase):
    """R4：幂等表并发写零异常 + db 连接复用。"""

    def test_idempotency_concurrent_access(self):
        errors = []

        def worker(n):
            try:
                for i in range(200):
                    with _IDEMPOTENCY_LOCK:
                        _IDEMPOTENCY[f"rid-{n}-{i}"] = (n, {"id": i})
                        _prune_idempotency()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        # 清理
        with _IDEMPOTENCY_LOCK:
            _IDEMPOTENCY.clear()

    def test_db_thread_local_reuse(self):
        """连续查询复用同一线程连接（不重建、不重跑 PRAGMA）。"""
        conn1 = db._thread_conn()
        conn2 = db._thread_conn()
        self.assertIs(conn1, conn2)
        # db() 上下文退出后连接仍缓存（复用生效）
        with db.db() as conn:
            conn.execute("SELECT 1")
        conn3 = db._thread_conn()
        self.assertIs(conn1, conn3)

    def test_db_connection_reset_on_path_change(self):
        """测试切换 DB_PATH 时，线程本地连接必须失效重建（避免旧库串线）。"""
        orig_path = db.DB_PATH
        conn1 = db._thread_conn()
        try:
            tmp = Path(__file__).resolve().parent / ".tmp_r6_switch.db"
            db.DB_PATH = tmp
            conn2 = db._thread_conn()
            self.assertIsNot(conn1, conn2)
        finally:
            db.close_thread_conn()  # 必须先关掉指向 tmp 的连接，才能 unlink
            db.DB_PATH = orig_path
            db._TLS.conn = None  # 线程本地复位，避免残留指向旧路径的连接
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


class TestR5Audit(unittest.TestCase):
    """R5：破坏性操作审计落盘。"""

    def test_audit_appends_line(self):
        target = auth._AUDIT_PATH
        try:
            target.unlink(missing_ok=True)
            auth.audit("test_delete", ip="127.0.0.1", detail="id=1")
            self.assertTrue(target.is_file())
            line = target.read_text(encoding="utf-8").strip()
            self.assertIn("127.0.0.1", line)
            self.assertIn("test_delete", line)
        finally:
            target.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
