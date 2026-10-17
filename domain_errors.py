"""领域层 4xx 语义的公共基类（ADR 0010 配套）。

领域模块不 import HTTP 层、也不直接写响应，用异常表达**可预期的用户侧错误**
（参数不合法、对象不存在、状态冲突），由 handler 适配层转成
`{"error": message}` + status。这样同一套规则被 CLI/导出/测试直接调用时，
错误语义不会丢。
"""
from __future__ import annotations


class DomainError(Exception):
    """可预期的用户侧错误：适配层输出 `{"error": message}` 与 `status`。"""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
