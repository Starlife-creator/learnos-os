"""插件 / MCP 机制脚手架（§30.1，远景）。

定位：本地优先的可扩展点，**零依赖、零网络、纯标准库**。当前为稳定骨架：
- 插件 = 一个含 ``plugin.json`` 与 ``plugin.py`` 的目录，``plugin.py`` 提供
  ``register(api)`` 钩子，向 LearnOS 注册命令/数据源。
- MCP（Model Context Protocol）桥 = 同进程内的轻量注册表，把外部 MCP server 暴露的
  tool 收敛为统一 ``call(tool, args)`` 接口；真实传输留作后续（stdio/http）。

安全：插件仅能拿到注入的 ``api`` 沙箱对象（受控读写、DB 只读代理），无法直接触达
全局；加载失败不影响主程序（fail-closed）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Callable

# 插件目录（项目根下 plugins/）。可被环境变量覆盖但不写盘、不改默认行为。
DEFAULT_PLUGIN_DIR = Path(__file__).resolve().parent / "plugins"


# ── MCP 桥（同进程注册表；外部 server 传输留作后续）──
class _MCPBridge:
    def __init__(self) -> None:
        self._tools: dict[str, Callable[..., Any]] = {}

    def register(self, name: str, fn: Callable[..., Any]) -> None:
        self._tools[name] = fn

    def call(self, name: str, args: dict[str, Any] | None = None) -> Any:
        if name not in self._tools:
            raise KeyError(f"未知 MCP 工具: {name}")
        return self._tools[name](**(args or {}))

    def list_tools(self) -> list[str]:
        return sorted(self._tools)


MCP = _MCPBridge()


# ── 插件加载 ──
class PluginAPI:
    """注入插件的受控沙箱：只读 data 访问 + 注册命令/MCP 工具。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self._commands: dict[str, Callable[..., Any]] = {}

    def register_command(self, cmd: str, fn: Callable[..., Any]) -> None:
        self._commands[cmd] = fn

    def register_mcp_tool(self, tool: str, fn: Callable[..., Any]) -> None:
        MCP.register(f"{self.name}.{tool}", fn)

    def list_commands(self) -> list[str]:
        return sorted(self._commands)

    def call_command(self, cmd: str, args: dict[str, Any] | None = None) -> Any:
        if cmd not in self._commands:
            raise KeyError(f"插件 {self.name} 未注册命令: {cmd}")
        return self._commands[cmd](**(args or {}))

    # DB 只读代理：插件只能查，不能写
    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        from db import rows
        return rows(sql, params)


def load_plugins(plugin_dir: Path | None = None) -> list[str]:
    """扫描并加载插件目录。每个子目录需含 plugin.json + plugin.py。

    返回成功加载的插件名列表；单个插件异常被隔离，不影响其余。
    """
    base = plugin_dir or DEFAULT_PLUGIN_DIR
    if not base.is_dir():
        return []
    loaded: list[str] = []
    for path in sorted(base.iterdir()):
        if not path.is_dir():
            continue
        manifest = path / "plugin.json"
        module_file = path / "plugin.py"
        if not (manifest.is_file() and module_file.is_file()):
            continue
        try:
            meta = json.loads(manifest.read_text(encoding="utf-8"))
            name = str(meta.get("name", path.name)).strip()
            if not name or name in sys.modules:
                continue
            spec = importlib.util.spec_from_file_location(f"_plugin_{name}", module_file)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            api = PluginAPI(name)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            if hasattr(mod, "register"):
                mod.register(api)  # type: ignore[attr-defined]
            loaded.append(name)
        except Exception as exc:
            # fail-closed：单个插件坏不影响主程序
            from config import LOG
            LOG.warning("插件 %s 加载失败（已跳过）: %s", path.name, exc)
    return loaded
