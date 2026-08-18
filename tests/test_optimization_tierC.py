"""Tier C 回归测试：P4b（导出令牌失败限流）。

守护点（对齐优化方案验收标准）：
- 连续 N 次错误令牌（非回环）→ 429；未超限仍为 401。
- 有效令牌不受限流影响，且即时清零该客户端失败计数。
- 回环地址阈值放宽（本机重试/测试不误伤）。
- 时间窗外的失败自动过期。
"""
import os
import sys
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import ratelimit  # noqa: E402
import resp  # noqa: E402
from handler_problems import ProblemsMixin  # noqa: E402

# 测试用阈值：非回环 5 次/窗、回环 12 次/窗（环境变量在调用时读取，可运行时覆盖）
_TEST_ENV = {
    "LEARNOS_RL_MAX_REMOTE": "5",
    "LEARNOS_RL_MAX_LOOPBACK": "12",
    "LEARNOS_RL_WINDOW": "60",
}


class _GuardHandler(ProblemsMixin):
    """最小 handler 替身：仅提供守卫所需属性。client_address 决定限流归因。"""

    def __init__(self, token: str = "", client_address=("10.0.0.5", 54321)):
        self.path = "/api/export"
        self.headers: dict[str, str] = {}
        if token:
            self.headers["X-Export-Token"] = token
        self.client_address = client_address
        self.status = None
        self.body = None

    def json_response(self, data, status: int = 200):
        self.status = status
        self.body = data


class TestRateLimitUnit(unittest.TestCase):
    """ratelimit 模块自身行为（不经过 handler）。"""

    def setUp(self):
        ratelimit.clear_all()
        resp.reset_error_counts()
        self._env = unittest.mock.patch.dict(os.environ, _TEST_ENV)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        ratelimit.clear_all()

    def test_remote_blocked_after_limit(self):
        ip = "10.0.0.5"
        for _ in range(5):
            self.assertFalse(ratelimit.register_failure(ip))  # 1..5 未超限
        self.assertTrue(ratelimit.register_failure(ip))        # 第 6 次超限
        self.assertTrue(ratelimit.is_blocked(ip))

    def test_loopback_relaxed_threshold(self):
        ip = "127.0.0.1"
        for _ in range(12):
            self.assertFalse(ratelimit.register_failure(ip))   # 1..12 未超限（放宽）
        self.assertTrue(ratelimit.register_failure(ip))        # 第 13 次才超限

    def test_clear_resets_client(self):
        ip = "10.0.0.9"
        for _ in range(4):
            ratelimit.register_failure(ip)
        ratelimit.clear(ip)
        # 清零后重新累计，前 5 次不再触发
        for _ in range(5):
            self.assertFalse(ratelimit.register_failure(ip))

    def test_window_expiry(self):
        with unittest.mock.patch.dict(os.environ, {"LEARNOS_RL_WINDOW": "0.2"}):
            ip = "10.0.0.7"
            for _ in range(3):
                ratelimit.register_failure(ip)
            time.sleep(0.3)
            # 窗口外失败被剪掉，下一次失败不计入旧账
            self.assertFalse(ratelimit.register_failure(ip))
            self.assertFalse(ratelimit.is_blocked(ip))


class TestGuardIntegration(unittest.TestCase):
    """_guard_export_token 接入后的端点行为（401 → 429 → 有效令牌恢复）。"""

    def setUp(self):
        ratelimit.clear_all()
        resp.reset_error_counts()
        self._env = unittest.mock.patch.dict(os.environ, _TEST_ENV)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        ratelimit.clear_all()

    def test_wrong_tokens_yield_401_then_429(self):
        for i in range(1, 7):
            h = _GuardHandler(token="wrong", client_address=("10.0.0.5", 1))
            self.assertFalse(h._guard_export_token())
            expected = 401 if i <= 5 else 429
            self.assertEqual(h.status, expected, f"第 {i} 次失败应为 {expected}")

    def test_valid_token_passes_and_resets_failures(self):
        for _ in range(4):  # 先攒 4 次失败（未超限）
            _GuardHandler(token="wrong", client_address=("10.0.0.5", 1))._guard_export_token()
        ok = _GuardHandler(token=config.EXPORT_TOKEN,
                           client_address=("10.0.0.5", 1))._guard_export_token()
        self.assertTrue(ok)
        # 有效令牌清零失败计数：再失败 5 次仍只是 401，不会 429
        for i in range(1, 6):
            h = _GuardHandler(token="wrong", client_address=("10.0.0.5", 1))
            h._guard_export_token()
            self.assertEqual(h.status, 401, f"重置后第 {i} 次失败应为 401")

    def test_loopback_not_rate_limited_at_remote_threshold(self):
        # 回环阈值 12：连打 12 次错误令牌全是 401（而非远程阈值的 429）
        for i in range(1, 13):
            h = _GuardHandler(token="wrong", client_address=("127.0.0.1", 1))
            self.assertFalse(h._guard_export_token())
            self.assertEqual(h.status, 401, f"回环第 {i} 次失败应为 401")


if __name__ == "__main__":
    unittest.main()
