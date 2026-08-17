"""LearnOS — 主入口。

启动本地 HTTP 服务器，提供个人学习辅助工具。
零第三方依赖，纯 Python 标准库实现。
"""
from __future__ import annotations

import os
import threading
import webbrowser
from http.server import ThreadingHTTPServer

from config import HOST, PORT, LOG, ALLOW_LAN, setup_logging
from db import init_db
from handler import Handler


def main() -> None:
    setup_logging()
    # §1.1/§16.6：非回环地址对外开放时，必须有意识地通过环境变量放行，
    # 否则打印醒目警告（导出已统一强制令牌，跨源/跨设备无法导出整库）。
    if HOST not in ("127.0.0.1", "localhost", "::1") and not ALLOW_LAN:
        LOG.warning("="*60)
        LOG.warning("安全警告：LearnOS 正监听 %s（非回环地址）。", HOST)
        LOG.warning("若需局域网/公网访问，请显式设置 LEARNOS_ALLOW_LAN=1 并自行承担风险。")
        LOG.warning("未设置时仍可启动，但导出端点已强制一次性令牌，外部无法抓取整库。")
        LOG.warning("="*60)
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
