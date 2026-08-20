"""LearnOS — 主入口。

启动本地 HTTP 服务器，提供个人学习辅助工具。
零第三方依赖，纯 Python 标准库实现。
"""
from __future__ import annotations

import os
import sys
import threading
import webbrowser
from http.server import ThreadingHTTPServer

import auth
from config import HOST, PORT, LOG, ALLOW_LAN, API_TOKEN, setup_logging
from db import init_db
from handler import Handler


def _check_exposed_token() -> bool:
    """R2 启动守卫：暴露模式必须配置 LEARNOS_API_TOKEN，否则拒绝启动。

    返回 True=可继续启动；False=应退出（已输出错误日志）。
    抽成独立函数便于测试（不真正起服务器）。
    """
    if auth.is_exposed() and not API_TOKEN:
        LOG.error("=" * 60)
        LOG.error("安全拒绝启动：LearnOS 正监听 %s（非回环地址，对网络开放）。", HOST)
        LOG.error("暴露模式下所有写/删/还原操作必须携带 Authorization: Bearer <LEARNOS_API_TOKEN>。")
        LOG.error("请设置环境变量 LEARNOS_API_TOKEN=<强随机串> 后重新启动（可用 `python -c \"import secrets;print(secrets.token_hex(32))\"` 生成）。")
        LOG.error("=" * 60)
        return False
    if HOST not in ("127.0.0.1", "localhost", "::1") and not ALLOW_LAN:
        LOG.warning("=" * 60)
        LOG.warning("安全提示：LearnOS 正监听 %s（非回环地址）。", HOST)
        LOG.warning("写操作已启用 Bearer 令牌鉴权（LEARNOS_API_TOKEN）；若为无意暴露请改回 127.0.0.1。")
        LOG.warning("=" * 60)
    return True


def main() -> None:
    setup_logging()
    # R2（P1-1 根因）：暴露模式必须显式配置 LEARNOS_API_TOKEN，否则拒绝启动。
    # 绝不静默回退到无认证——「暴露但无 token」曾是可被任意网络客户端写库的头号漏洞。
    if not _check_exposed_token():
        sys.exit(1)
    init_db()
    # C7：每日首次启动自动备份（幂等，失败不阻塞启动）
    try:
        from backup import auto_backup_if_due
        auto_backup_if_due()
    except Exception as exc:
        LOG.warning("自动备份失败（可忽略）: %s", exc)

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    LOG.info("LearnOS 已启动: %s", url)
    LOG.info("按 Ctrl+C 停止。")
    LOG.info("数据保存在 SQLite 数据库中。")

    if not os.environ.get("LEARNOS_NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("正在停止...")
    except OSError as exc:
        addr_in_use = (
            exc.errno in (98, 10048)
            or "address already in use" in str(exc).lower()
            or "已在使用" in str(exc)
            or "已被使用" in str(exc)
        )
        if addr_in_use:
            LOG.error("端口 %d 已被占用，请更换端口: set LEARNOS_PORT=9000 && python app.py", PORT)
        else:
            LOG.error("服务器错误: %s", exc)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
