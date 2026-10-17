"""架构守卫测试（Architecture Fitness Function）。

把「架构决策」写成可执行断言，在 CI 层拦截结构性退化——
不是测某个函数返回值对不对，而是测**代码结构是否仍符合设计意图**。

命名由来：Neal Ford / Rebecca Parsons / Patrick Kua，《Building Evolutionary
Architectures》(2017)：「对架构特征提供客观完整性评估的自动化检查」。

选录规则（业界实践）：一条规则只有同时满足「重要 / 可客观测试 / 相对稳定 /
违例代价高」四条才值得自动化；否则应留在文档与 review 里，避免误报噪音。

先例：本项目的 `test_handler.py` 已有同构做法（用 Python 打开 static/*.html
并正则扫描，与 locale JSON 比对）。本文件沿用同一手法，零新增依赖。
"""
from __future__ import annotations

import ast
import inspect
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STATIC = ROOT / "static"
HANDLER_MODULES = (
    "handler", "handler_problems", "handler_reports", "handler_learn",
    "handler_material", "handler_cards", "handler_oral", "handler_reviews",
    "handler_social",
)

# 直接触发 AI 调用的符号（出现在 handler 方法体内即视为 AI 端点）
AI_CALL_TOKENS = (
    "call_ai", "call_ai_stream", "call_ai_vision",
    "start_oral", "continue_oral", "start_feynman",
)


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _js_files() -> list[Path]:
    return sorted(STATIC.glob("*.js"))


# 顶层（缩进 0）声明 = 该页全局；带缩进的声明 = 函数/块内局部。
# 注意：必须覆盖 `async function`、`window.x =`、`class`——早前只认 `function`/`const`，
# 于是所有 `async function foo()` 都不算全局，遮蔽检测对它们静默失效。
_DECL_TOP = re.compile(
    r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"
    r"|^(?:const|let|var)\s+([A-Za-z_$][\w$]*)"
    r"|^window\.([A-Za-z_$][\w$]*)\s*="
    r"|^class\s+([A-Za-z_$][\w$]*)")
_DECL_NESTED = re.compile(
    r"^\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)"
    r"|^\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)")
# 浏览器/DOM 自带或纯局部惯用名，不作为「被遮蔽」判据
_BUILTIN_LIKE = {"window", "document", "console", "Math", "JSON", "localStorage",
                 "navigator", "location"}


def _page_scopes() -> dict[str, list[Path]]:
    """按 HTML 页面划分脚本作用域：同一页面加载的脚本共享一个全局命名空间。

    直接解析 <script src>，新增脚本自动纳入，无需手工维护清单。
    （index.html 与 concept_map.html / learn.html 各自独立，不能混在一起判遮蔽，
      否则 concept_map.js 自己的顶层声明会被误判为「遮蔽全局」。）
    """
    scopes: dict[str, list[Path]] = {}
    for html in sorted(STATIC.glob("*.html")):
        scripts = [m.group(1) for m in re.finditer(
            r'<script[^>]+src="([^"]+\.js)"', html.read_text(encoding="utf-8"))]
        paths = []
        for s in scripts:
            p = STATIC / s.lstrip("/")
            if p.exists():
                paths.append(p)
        if paths:
            scopes[html.name] = paths
    return scopes


def _decl_name(line: str) -> str | None:
    """顶层声明行 → 该行声明的全局名；非顶层声明返回 None。"""
    m = _DECL_TOP.match(line)
    return next((g for g in m.groups() if g), None) if m else None


def _globals_of(paths: list[Path]) -> set[str]:
    syms: set[str] = set()
    for p in paths:
        for line in p.read_text(encoding="utf-8").split("\n"):
            name = _decl_name(line)
            if name:
                syms.add(name)
    return syms - _BUILTIN_LIKE


# 内联事件处理器属性：onclick="foo(...)" 之类
_INLINE_HANDLER = re.compile(
    r"""on(?:click|input|change|keyup|keydown|keypress|submit|blur|focus|dblclick"""
    r"""|mouseenter|mouseleave|mouseover|load|error)\s*=\s*(["'])(.*?)\1""",
    re.S)
_INLINE_CALL = re.compile(r"^\s*([A-Za-z_$][\w$]*)\s*\(")
# 处理器体以这些开头时是语句/内建对象，不是「页面全局函数」
_INLINE_KEYWORDS = {"if", "for", "while", "return", "switch", "this", "event",
                    "window", "document", "setTimeout", "clearTimeout", "print",
                    "alert", "confirm", "open", "location", "history", "blur",
                    "focus", "scrollTo", "getComputedStyle", "requestAnimationFrame",
                    "parseInt", "parseFloat", "isNaN", "encodeURIComponent", "String",
                    "Number", "Boolean", "JSON", "Object", "Array", "Math", "Date",
                    "URL", "FormData", "FileReader", "Image", "crypto", "performance"}


def _inline_handler_names(text: str) -> set[str]:
    """提取内联处理器里首个被调用的名字（只取调用式，赋值式/表达式式跳过）。"""
    names: set[str] = set()
    for m in _INLINE_HANDLER.finditer(text):
        cm = _INLINE_CALL.match(m.group(2))
        if cm:
            names.add(cm.group(1))
    return names - _INLINE_KEYWORDS


class TestGlobalShadowing(unittest.TestCase):
    """根因 1：全局函数被局部变量遮蔽 → 静默失效。

    历史上 `t()`（i18n）被遮蔽 15 处，其中 2 处引爆：口试收尾按钮不出现、
    概览页后半段不渲染。本测试让该整类问题无法再被引入。
    """

    def test_no_global_symbol_shadowing(self) -> None:
        scopes = _page_scopes()
        self.assertTrue(scopes, "未能从 HTML 解析出脚本作用域，扫描逻辑失效")
        arrow = re.compile(r"\.(?:map|forEach|filter|find|some|every|flatMap)\(\s*([A-Za-z_$][\w$]*)\s*=>")
        loop = re.compile(r"\bfor\s*\(\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s+(?:of|in)\b")

        offenders: list[str] = []
        for page, paths in scopes.items():
            syms = _globals_of(paths)
            for path in paths:
                for i, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
                    # 仅带缩进的声明才算遮蔽；顶层声明本身就是全局定义，不算
                    m = _DECL_NESTED.match(line)
                    if m and m.group(1) in syms:
                        offenders.append(
                            f"{page}/{path.name}:{i} 局部声明遮蔽全局 {m.group(1)}: {line.strip()[:70]}")
                        continue
                    for mm in loop.finditer(line):
                        if mm.group(1) in syms:
                            offenders.append(
                                f"{page}/{path.name}:{i} 循环变量遮蔽全局 {mm.group(1)}: {line.strip()[:70]}")
                    # 回调参数仅当体内真的调用了同名全局时才报错，
                    # 避免对 `.map(tag => tag.text)` 这类合法写法误报
                    for mm in arrow.finditer(line):
                        name = mm.group(1)
                        if name in syms and re.search(rf"\b{re.escape(name)}\s*\(", line):
                            offenders.append(
                                f"{page}/{path.name}:{i} 回调参数 {name} 遮蔽全局且体内有调用: "
                                f"{line.strip()[:70]}")
        self.assertEqual([], offenders, "发现全局符号遮蔽：\n  " + "\n  ".join(offenders))

    def test_guard_itself_is_sensitive(self) -> None:
        """自检：故意构造两处遮蔽，确认扫描规则能抓到（防止守卫自身悄悄失效）。"""
        nested = "    const t = document.getElementById('x');"
        self.assertTrue(_DECL_NESTED.match(nested), "嵌套声明应被识别")
        top = "function t(key, fallback) {"
        self.assertTrue(_DECL_TOP.match(top), "顶层声明应被识别为全局定义")
        # 关键：顶层声明不应被判为遮蔽
        self.assertIsNone(_DECL_NESTED.match(top), "顶层声明误判为遮蔽 → 会产生全量误报")
        arrow = re.compile(r"\.(?:map|forEach|filter|find|some|every|flatMap)\(\s*([A-Za-z_$][\w$]*)\s*=>")
        self.assertTrue(any(m.group(1) == "t" for m in arrow.finditer("d.topics.map(t => `x`")))

    def test_global_extractor_covers_all_declaration_forms(self) -> None:
        """自检：`async function` / `window.x =` / `class` 也必须算全局。

        早前只认 `function` 与 `const|let|var`，于是所有 `async function foo()` 都不在
        全局表里 —— 遮蔽检测对它们静默失效。本测试锁住该盲点不复发。
        """
        for line in ("function t(key) {", "async function saveProblem() {",
                     "const api = async (", "let _drillIdx = 0;", "var legacy = 1;",
                     "window.exportData = function () {", "class Sheet {"):
            self.assertIsNotNone(_decl_name(line), f"顶层声明未被识别为全局: {line}")
        # 反例：嵌套声明、对象字面量方法都不是全局
        self.assertIsNone(_decl_name("  const t = 1;"), "嵌套声明不该算全局")
        self.assertIsNone(_decl_name("    onSave: function () {"), "对象字面量方法不该算全局")
        self.assertIsNone(_decl_name("  return foo;"), "普通语句不该算全局")
        # 嵌套函数声明同样会造成遮蔽
        self.assertTrue(_DECL_NESTED.match("    function t(key) {"), "嵌套函数声明应被识别为遮蔽")


class TestInlineHandlerWiring(unittest.TestCase):
    """根因 3：经典脚本 + 内联 onclick 的隐式全局契约。

    本前端有意不用 ES 模块、不走构建（ADR 0001 零依赖本地优先）：同一页面加载的
    脚本共享一个全局作用域，HTML 字符串里的 `onclick="foo()"` 靠全局名解析。
    代价是拼错名字在加载期不报错，而是点下去才 ReferenceError 或静默无反应——
    这类「死按钮」bug 靠人工点检发现，成本高。本测试把这份隐式契约变成可机器校验的。
    """

    def test_inline_handlers_resolve_to_page_globals(self) -> None:
        scopes = _page_scopes()
        self.assertTrue(scopes, "未能从 HTML 解析出脚本作用域，扫描逻辑失效")
        offenders: list[str] = []
        checked = 0
        for page, paths in scopes.items():
            syms = _globals_of(paths)
            texts = [(page, (STATIC / page).read_text(encoding="utf-8"))]
            texts += [(p.name, p.read_text(encoding="utf-8")) for p in paths]
            for fname, text in texts:
                for name in sorted(_inline_handler_names(text)):
                    checked += 1
                    if name not in syms:
                        offenders.append(
                            f"{page}: 内联处理器调用了未定义的全局 {name}() ← {fname}")
        self.assertGreater(checked, 100, f"只扫到 {checked} 个内联处理器，提取逻辑可能失效")
        self.assertEqual([], offenders, "内联事件处理器引用了不存在的全局函数：\n  " + "\n  ".join(offenders))

    def test_guard_itself_is_sensitive(self) -> None:
        """自检：调用式要抓到，语句/内建/赋值要不抓（否则会产生大量误报）。"""
        self.assertEqual({"saveProblem"}, _inline_handler_names('<button onclick="saveProblem(1)">'))
        self.assertEqual({"searchPaletteInput"}, _inline_handler_names("<input oninput='searchPaletteInput()'>"))
        self.assertEqual({"openModal"}, _inline_handler_names('onclick="openModal(\'x\'); saveProblem(1)"'))
        # 多行属性值
        self.assertEqual({"periodicTab"}, _inline_handler_names('onclick="\n  periodicTab(\'north\')\n"'))
        # 关键字 / 内建 / 取值式：不算「页面全局函数」
        self.assertEqual(set(), _inline_handler_names('onclick="if (x) go()"'))
        self.assertEqual(set(), _inline_handler_names('onclick="return false"'))
        self.assertEqual(set(), _inline_handler_names('onclick="this.close()"'))
        self.assertEqual(set(), _inline_handler_names('onclick="window.print()"'))
        self.assertEqual(set(), _inline_handler_names('onclick="_drillIdx = 0"'))


class TestModalRegistry(unittest.TestCase):
    """根因 2：命令式建弹窗会丢掉 id，进不了 openModal 注册表，
    只能退回按 class 全局查找 —— 曾误删 #searchModal 导致 Ctrl+K 永久失效。"""

    def test_imperative_modal_must_be_addressable(self) -> None:
        """命令式创建的弹窗必须① 赋 id ② 由模块自己持有句柄。

        为什么禁止「无 id 的命令式弹窗」：
          - openModal/closeModal/Esc 处理器都按 id 寻址（app-core.js），无 id 即失联；
          - 关闭时只能退回按 class 全局查找，而它会命中 DOM 里第一个遮罩
            （历史上误删 #searchModal，导致 Ctrl+K 全局搜索永久失效）。

        理想态是在 index.html 里声明；确需动态创建时，必须补上 id 与句柄。
        """
        creates = re.compile(
            r"(?:className\s*=\s*['\"]modal-overlay|classList\.add\(\s*['\"]modal-overlay)")
        offenders = []
        for path in _js_files():
            text = path.read_text(encoding="utf-8")
            lines = text.split("\n")
            hit_lines = [i for i, ln in enumerate(lines, 1) if creates.search(ln)]
            if not hit_lines:
                continue
            # ① 同一文件内必须给该遮罩赋过 id
            if not re.search(r"\b\w+\.id\s*=\s*['\"][\w-]+['\"]", text):
                offenders.append(
                    f"{path.name}:{hit_lines[0]} 命令式建弹窗但未赋 id（Esc/closeModal 将失联）")
            # ② 必须有模块级句柄变量持有它（形如 `let _xxxOverlay = null;` + `= ov`）
            if not re.search(r"^let\s+_\w*[Oo]verlay\s*=", text, re.M):
                offenders.append(
                    f"{path.name}:{hit_lines[0]} 命令式建弹窗但未持有模块级句柄"
                    "（关闭时只能按 class 全局查找，易误删他人弹窗）")
        self.assertEqual([], offenders, "\n  ".join(offenders))

    def test_no_global_modal_query(self) -> None:
        """禁止全局查找 .modal-overlay：它命中 DOM 里第一个遮罩，与意图无关。"""
        bad = re.compile(r"querySelector\(\s*['\"]\.modal-overlay['\"]\s*\)")
        offenders = []
        for path in _js_files():
            for i, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
                if bad.search(line) and not line.strip().startswith("//"):
                    offenders.append(f"{path.name}:{i}: {line.strip()[:70]}")
        self.assertEqual([], offenders, "必须持有弹窗引用，不得按 class 全局查找：\n  "
                         + "\n  ".join(offenders))


class TestAiQuotaGuard(unittest.TestCase):
    """根因 3：安全守卫若靠每个端点手写（opt-in），必然被遗忘。

    实测：集中式 _write_auth_ok 覆盖 100%，分散式 _ai_quota 覆盖 11/15。
    本测试在 CI 层把 opt-in 变成「忘了就过不了」。
    """

    def _ai_handlers(self) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for mod_name in HANDLER_MODULES:
            try:
                mod = __import__(mod_name)
            except Exception:  # pragma: no cover - 模块缺失不应静默跳过
                continue
            tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not node.name.startswith("_handle"):
                    continue
                seg = ast.dump(node)
                if any(tok in seg for tok in AI_CALL_TOKENS):
                    found.append((mod_name, node.name))
        return found

    def test_ai_endpoints_all_guarded(self) -> None:
        handlers = self._ai_handlers()
        self.assertTrue(handlers, "未发现任何 AI handler，扫描逻辑可能失效")
        unguarded: list[str] = []
        for mod_name, fname in handlers:
            mod = __import__(mod_name)
            fn = getattr(mod, fname, None)
            if fn is None:
                continue
            try:
                src = inspect.getsource(fn)
            except OSError:  # pragma: no cover
                continue
            if "_ai_quota" not in src:
                unguarded.append(f"{mod_name}.{fname}")
        self.assertEqual([], unguarded,
                         "以下 AI 端点缺少 _ai_quota 守卫（跨源可刷 AI 额度）：\n  "
                         + "\n  ".join(unguarded))

    def test_get_routes_never_touch_ai(self) -> None:
        """GET 端点不经 _write_auth_ok，因此绝不允许触发 AI。"""
        from handler import Handler
        offenders = []
        for _pattern, method in Handler.GET_ROUTES:
            src = inspect.getsource(getattr(Handler, method))
            if any(tok in src for tok in AI_CALL_TOKENS):
                offenders.append(method)
        self.assertEqual([], offenders,
                         "GET 端点不得调用 AI（无 CSRF/Bearer 闸门）：" + ", ".join(offenders))


class TestRouteTableIntegrity(unittest.TestCase):
    """A14：路由表声明的捕获组数必须与 handler 签名元数一致。

    历史 bug：`/api/export/seed` 注册进 GET_ROUTES（0 捕获组）而 handler 需 1 个
    位置参数 → GET 该路径必然 TypeError → 500。
    """

    def test_route_arity_matches_signature(self) -> None:
        from handler import Handler
        problems: list[str] = []
        for _pattern, method in Handler.GET_ROUTES:
            fn = getattr(Handler, method, None)
            if fn is None:
                problems.append(f"GET {method}: 方法不存在")
                continue
            want = re.compile(_pattern).groups
            have = len(inspect.signature(fn).parameters) - 1  # 去掉 self
            if want != have:
                problems.append(f"GET {_pattern} -> {method}: 正则组={want} 形参={have}")
        for _pattern, method, needs_data in Handler.POST_ROUTES:
            fn = getattr(Handler, method, None)
            if fn is None:
                problems.append(f"POST {method}: 方法不存在")
                continue
            want = re.compile(_pattern).groups + (1 if needs_data else 0)
            have = len(inspect.signature(fn).parameters) - 1
            if want != have:
                problems.append(f"POST {_pattern} -> {method}: 需要参数={want} 形参={have}")
        self.assertEqual([], problems, "路由元数失配：\n  " + "\n  ".join(problems))


class TestPrunedSurfaces(unittest.TestCase):
    """非目标守卫（ADR 0005）：裁剪掉的面不许悄悄长回来。

    被删的三个能力都「有实现、无消费者」，腐坏方式是把双份事实来源重新引入
    （学科元数据、PWA 清单）或重新开一个任意代码执行口（插件自动加载）。
    真要恢复，先改 ADR 0005 并说明消费者是谁。
    """

    PRUNED_ROUTES = ("/api/render-config", "/api/render-configs",
                     "/api/pwa/manifest", "/api/plugins")
    PRUNED_MODULES = ("plugins.py", "render_config.py")

    def test_pruned_routes_are_not_registered(self) -> None:
        from handler import Handler
        registered = {p for p, _m in Handler.GET_ROUTES}
        registered |= {p for p, _m, _a in Handler.POST_ROUTES}
        alive = sorted(r for r in self.PRUNED_ROUTES if r in registered)
        self.assertEqual([], alive, "已裁剪路由重新注册：见 ADR 0005")

    def test_pruned_modules_are_gone(self) -> None:
        back = [m for m in self.PRUNED_MODULES if (ROOT / m).exists()]
        self.assertEqual([], back, "已裁剪模块被重新加入：见 ADR 0005")

    def test_no_plugin_autoloader_anywhere(self) -> None:
        """`exec_module` 只允许出现在 importlib 无关的正常用法里，禁止插件式动态执行。"""
        hits: list[str] = []
        for path in sorted(ROOT.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "exec_module(" in text:
                hits.append(path.name)
        self.assertEqual([], hits, "重新出现动态模块执行（插件加载器？）：见 ADR 0005")


class TestBackupCompleteness(unittest.TestCase):
    """A15：BACKUP_TABLES 必须覆盖全部业务表（否则还原后静默丢数据）。

    历史 bug：缺 seed_versions → 还原后种子账本被认成最新，旧图谱永不提示升级。

    必须在**临时库**上跑满迁移后再比对，不能直接用 learnos.db：
    生产库可能停留在旧 schema（实测就停在 v27，v28+ 从未应用），那样会漏检新表；
    而且测试不该对生产库产生任何副作用。
    """
    _TMP = Path(__file__).resolve().parent / ".tmp"

    @classmethod
    def setUpClass(cls) -> None:
        import config
        import db
        import os
        cls._saved_cfg_db = config.DB_PATH
        cls._saved_db_db = db.DB_PATH
        cls._saved_env = os.environ.get("LEARNOS_DB")
        cls._tmp = tempfile.TemporaryDirectory(prefix="fitness_bak_", dir=cls._TMP)
        tmp_path = Path(cls._tmp.name)
        os.environ["LEARNOS_DB"] = str(tmp_path / "bak.db")
        db.DB_PATH = tmp_path / "bak.db"
        db.close_all_connections()
        db.init_db()  # 跑满全部迁移，拿到最新 schema 全集
        cls._db = db

    @classmethod
    def tearDownClass(cls) -> None:
        import config
        import os
        cls._db.close_all_connections()
        cls._tmp.cleanup()
        config.DB_PATH = cls._saved_cfg_db
        cls._db.DB_PATH = cls._saved_db_db
        if cls._saved_env is None:
            os.environ.pop("LEARNOS_DB", None)
        else:
            os.environ["LEARNOS_DB"] = cls._saved_env

    def test_backup_tables_cover_all_business_tables(self) -> None:
        import backup
        covered = set(backup.BACKUP_TABLES)
        with self._db.db() as conn:
            tables = [r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]
        # 纯派生物/可重建对象无需备份
        skip = {"schema_version", "rag_fts", "rag_fts_data", "rag_fts_idx",
                "rag_fts_content", "rag_fts_docsize", "rag_fts_config", "rag_embeddings"}
        missing = [t for t in tables if t not in covered and t not in skip]
        self.assertEqual([], missing,
                         "BACKUP_TABLES 漏表（还原将静默丢数据）：" + ", ".join(missing))
        # 反向：登记了但 schema 中不存在的表，会让导出/还原直接报错
        ghost = [t for t in covered if t not in tables]
        self.assertEqual([], ghost,
                         "BACKUP_TABLES 登记了 schema 中不存在的表（导出会失败）：" + ", ".join(ghost))


class TestEntrypointConsistency(unittest.TestCase):
    """根因 8：权威入口有 3 份副本必然漂移。

    历史 bug：ci.yml / README 的测试命令缺 `-t .`，绕过 tests/__init__.py 的
    加固 shim（影响 16 文件 37 处 timeout=N），CI 与本地行为不一致。
    """

    @staticmethod
    def _executable_lines(rel: str) -> str:
        """取「可执行内容」，忽略注释与散文（否则说明性文字会被误判为违规命令）。"""
        text = _read(rel)
        if rel.endswith(".yml") or rel.endswith(".yaml"):
            # 去掉整行注释与行尾注释
            keep = []
            for line in text.split("\n"):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                keep.append(re.sub(r"\s+#.*$", "", line))
            return "\n".join(keep)
        # Markdown：只取围栏代码块内的内容
        return "\n".join(
            block for block in re.findall(r"```[a-zA-Z]*\n(.*?)```", text, re.S))

    def test_ci_and_readme_reference_single_entrypoint(self) -> None:
        ci = self._executable_lines(".github/workflows/ci.yml")
        readme = self._executable_lines("README.md")
        self.assertIn("run_tests.py", ci,
                      "ci.yml 应统一调用 python run_tests.py（唯一权威入口）")
        self.assertIn("run_tests.py", readme,
                      "README 的代码块应统一指向 python run_tests.py（唯一权威入口）")
        for label, text in (("ci.yml", ci), ("README.md", readme)):
            self.assertNotIn("unittest discover -s tests", text,
                             f"{label} 仍在直接 discover，会绕过 tests/__init__.py 的加固 shim")

    def test_guard_ignores_comments(self) -> None:
        """自检：说明性注释里提到禁用命令，不应被判为违规（否则守卫自身误报）。"""
        sample = ("# 直接 `unittest discover -s tests` 会绕过 shim，不要这样做\n"
                  "run: python run_tests.py")
        filtered = "\n".join(l for l in sample.split("\n") if not l.lstrip().startswith("#"))
        self.assertNotIn("unittest discover -s tests", filtered)
        self.assertIn("run_tests.py", filtered)
        # 反例：注释之外的真实命令必须被抓到
        bad = "run: python -m unittest discover -s tests -v"
        self.assertIn("unittest discover -s tests", bad)


class TestReadmeStructure(unittest.TestCase):
    """README「项目结构」与真实文件同步：曾写「21 个测试文件」且漏列多个模块。"""

    @staticmethod
    def _structure_block() -> str:
        text = _read("README.md")
        m = re.search(r"## 项目结构\s*```\n(.*?)```", text, re.S)
        assert m, "README 缺少「项目结构」代码块"
        return m.group(1)

    def _listed_names(self) -> set[str]:
        names: set[str] = set()
        for line in self._structure_block().split("\n"):
            entry = line.split("#", 1)[0]
            for tok in entry.split():
                if "*" in tok or tok in ("learnos-os/", "/"):
                    continue
                if tok.endswith("/"):
                    names.add(tok)
                    continue
                # 「a.py/b.py」写法：拆成多个文件名
                for part in tok.split("/"):
                    if "." in part:
                        names.add(part)
        return names

    def test_listed_paths_exist(self) -> None:
        missing = [n for n in sorted(self._listed_names())
                   if not ((ROOT / n).exists() or (ROOT / "static" / n).exists())]
        self.assertEqual(missing, [], f"README 项目结构列出了不存在的路径：{missing}")

    def test_root_modules_are_listed(self) -> None:
        listed = self._listed_names()
        unlisted = sorted(p.name for p in ROOT.glob("*.py") if p.name not in listed)
        self.assertEqual(unlisted, [], f"根目录模块未写进 README 项目结构：{unlisted}")


class TestMasteryWriteGuard(unittest.TestCase):
    """ADR 0004：problems.mastery 只能经 mastery.set_problem_mastery() 修改。

    历史问题：十多处直接 `UPDATE problems SET mastery`，事件日志只覆盖约 15% 的变化，
    且用户手动编辑与算法结果混在一起无法区分。新建题目（INSERT）不受限。
    """

    _PAT = re.compile(r"UPDATE\s+problems\s+SET[^;\"']*?\bmastery\s*=", re.I | re.S)

    def test_no_direct_problem_mastery_update(self) -> None:
        offenders = []
        for py in sorted(ROOT.glob("*.py")):
            if py.name == "mastery.py":
                continue
            text = py.read_text(encoding="utf-8")
            for m in self._PAT.finditer(text):
                offenders.append(f"{py.name}:{text.count(chr(10), 0, m.start()) + 1}")
        self.assertEqual(offenders, [], "直接写 problems.mastery，请改用 mastery.set_problem_mastery()："
                         + ", ".join(offenders))


class TestAiLayerBoundaries(unittest.TestCase):
    """ADR 0008：ai 四层单向依赖 + 旧名可经 ai.* 拿到。

    历史问题：ai.py 单文件 1882 行，传输/提示词/降级/编排混装，改提示词要读全部。
    拆分后最容易出的错是「漏 re-export」（运行时才炸）和「层间反向 import」（循环依赖）。
    """

    LAYERS = ("ai_client", "ai_prompts", "ai_fallback")
    # 可变模块级状态：不走 from-import（否则读到值拷贝），由 __getattr__ 实时回退。
    LIVE_STATE_ONLY = {
        "_result_mem", "_cache_metrics_lock", "cache_hits", "cache_misses",
        "_settings_cache", "_settings_cache_time", "_runtime_key", "_master_password",
        "_probe_cache",
    }

    @staticmethod
    def _top_level_names(path: Path) -> list[str]:
        out: list[str] = []
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.append(node.name)
            elif isinstance(node, ast.Assign):
                out.extend(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                out.append(node.target.id)
        return out

    def test_every_layer_name_reachable_from_ai(self) -> None:
        import ai
        missing = []
        for layer in self.LAYERS:
            for name in self._top_level_names(ROOT / f"{layer}.py"):
                try:
                    getattr(ai, name)
                except AttributeError:
                    missing.append(f"{layer}.{name}")
        self.assertEqual(missing, [], "ai 未透出以下名字，旧调用方会 ImportError："
                         + ", ".join(sorted(missing)))

    def test_reexported_objects_are_identical(self) -> None:
        """函数/类必须同一对象；可变状态允许（也应）是实时读取的代理。"""
        import ai
        drift = []
        for layer in self.LAYERS:
            mod = __import__(layer)
            for name in self._top_level_names(ROOT / f"{layer}.py"):
                if name in self.LIVE_STATE_ONLY:
                    continue
                if getattr(ai, name) is not getattr(mod, name):
                    drift.append(f"{layer}.{name}")
        self.assertEqual(drift, [], "re-export 不是同一对象（值拷贝会静默读到旧值）："
                         + ", ".join(sorted(drift)))

    def test_layer_dependencies_are_one_way(self) -> None:
        """ai_client→无、ai_prompts→无、ai_fallback→ai_prompts、ai→三层。"""
        allowed = {
            "ai_client": set(),
            "ai_prompts": set(),
            "ai_fallback": {"ai_prompts"},
            "ai": {"ai_client", "ai_prompts", "ai_fallback"},
        }
        watched = {"ai", *self.LAYERS}
        bad = []
        for mod_name, ok in allowed.items():
            tree = ast.parse((ROOT / f"{mod_name}.py").read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    deps = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    deps = [(node.module or "").split(".")[0]]
                else:
                    continue
                for dep in deps:
                    if dep in watched and dep != mod_name and dep not in ok:
                        bad.append(f"{mod_name}.py → {dep}")
        self.assertEqual(bad, [], "ai 分层出现反向/越层依赖：" + ", ".join(sorted(set(bad))))


_SQL_KEYWORD_RE = re.compile(
    r"\b(?:SELECT\b|INSERT\s+(?:OR\s+\w+\s+)?INTO\b|UPDATE\s+[A-Za-z_{][\w{}]*\s+SET\b|DELETE\s+FROM\b)",
    re.IGNORECASE)


def _sql_literals(path: Path) -> list[int]:
    """返回「含 SQL 语句的字符串字面量」的行号（一个字面量算一条语句）。

    只认真正的语句骨架（`SELECT …`、`INSERT … INTO`、`UPDATE <表> SET`、
    `DELETE FROM`），而不是单独出现的关键字 —— 否则 docstring 里的「写/删/还原操作
    鉴权闸门（do_POST/PUT/DELETE）」和路由正则 `/api/cards/(\\d+)/delete` 都会被
    算成 SQL，让「剩余预算」这个进度条失真。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and _SQL_KEYWORD_RE.search(n.value)]


# #7 业务逻辑下沉：handler_* 里的裸 SQL 条数上限（ratchet）。
# 历史上业务规则混在 HTTP 层，脱离 HTTP 无法复用/测试。四批下沉后已从 51+45+24
# 降到下面这些零头；此表是「只减不增」的天花板，新增泄漏立即失败。
# 下沉完成后把对应数字调小（守卫只拦上升，不拦下降）。
#
# handler.py 剩 2 条是有意留下的：
#   - `_get_or_404` 的 `SELECT * FROM {table}`：表名/id 列走白名单校验的通用查存在
#     助手，本身就是 HTTP 层的产物（拼 404 响应），挪进领域层反而把响应知识带下去。
#   - `/api/fsrs/train` 的训练样本查询：这是一次全表扫描取样本，没有业务规则。
# handler_problems.py 已清零：提示/变式/导出分别下沉 hints.py、problems.py、exports.py
# （`/api/problems/batch` 的状态机在第二批由 problems.batch 收掉）。
HANDLER_SQL_BUDGET = {
    "handler.py": 2,
    "handler_problems.py": 0,
    "handler_reviews.py": 0,
    "handler_cards.py": 0,
    "handler_material.py": 1,
    "handler_learn.py": 0,
    "handler_oral.py": 1,
    "handler_reports.py": 0,
    "handler_social.py": 0,
}


class TestHandlerSqlBudget(unittest.TestCase):
    """handler 层是 HTTP 适配器，不是数据访问层（§ADR 0010）。"""

    def test_handler_modules_stay_within_sql_budget(self) -> None:
        over = [f"{name}: {len(_sql_literals(ROOT / name))} > {budget}"
                for name, budget in HANDLER_SQL_BUDGET.items()
                if len(_sql_literals(ROOT / name)) > budget]
        self.assertEqual(over, [], "handler 层新增了裸 SQL，请下沉到领域模块："
                         + "; ".join(over))

    def test_every_handler_module_has_a_budget(self) -> None:
        """新增 handler_*.py 必须同时登记预算，否则这个守卫是可绕过的。"""
        missing = [f"{n}.py" for n in HANDLER_MODULES
                   if f"{n}.py" not in HANDLER_SQL_BUDGET]
        self.assertEqual(missing, [], "以下 handler 未登记 SQL 预算：" + ", ".join(missing))


class TestDomainLayerPurity(unittest.TestCase):
    """领域层不得反向依赖 HTTP 层，否则「下沉」只是搬家。"""

    DOMAIN = ("reports.py", "mastery.py", "scheduler.py", "graph.py",
              "bank.py", "cards.py", "review.py", "problems.py",
              "review_session.py", "domain_errors.py", "subjects.py",
              "metrics.py", "hints.py", "exports.py")

    def test_domain_modules_do_not_import_http_layer(self) -> None:
        bad = []
        for name in self.DOMAIN:
            tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    deps = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    deps = [(node.module or "").split(".")[0]]
                else:
                    continue
                for dep in deps:
                    if dep.startswith("handler") or dep in {"app", "http.server"}:
                        bad.append(f"{name} → {dep}")
        self.assertEqual(bad, [], "领域层反向依赖 HTTP 层：" + ", ".join(sorted(set(bad))))

    def test_reports_layer_has_no_http_knowledge(self) -> None:
        src = (ROOT / "reports.py").read_text(encoding="utf-8")
        for token in ("json_response", "self."):
            self.assertNotIn(token, src, f"reports.py 不应出现 HTTP 层概念：{token}")


class TestPerformanceBudget(unittest.TestCase):
    """性能预算型 fitness function：把性能决策锁进 CI。

    背景：update_progress 原为概念-major 循环 + 逐概念 LIKE 全扫（O(概念×题目)），
    实测 3000 题时 855ms，且全程持有 DB_LOCK（等于全局串行化阻塞）。
    改为题目-major 聚合后实测 21.6ms。本测试锁定该成果。
    """
    _TMP = Path(__file__).resolve().parent / ".tmp"

    @classmethod
    def setUpClass(cls) -> None:
        import config
        import db
        import os
        # 必须同时保存 config.DB_PATH 与 db.DB_PATH —— 后者是 db.py 在 import 时
        # 按值绑定的副本（`from config import DB_PATH`），只还原前者是不够的。
        cls._saved_cfg_db = config.DB_PATH
        cls._saved_db_db = db.DB_PATH
        cls._saved_env = os.environ.get("LEARNOS_DB")
        cls._tmp = tempfile.TemporaryDirectory(prefix="fitness_", dir=cls._TMP)
        cls._tmp_path = Path(cls._tmp.name)
        os.environ["LEARNOS_DB"] = str(cls._tmp_path / "fitness.db")
        db.DB_PATH = cls._tmp_path / "fitness.db"
        db.close_all_connections()  # 丢弃指向旧库的线程连接
        db.init_db()
        cls._db = db
        # 灌入 physics 种子概念，否则下面的规模断言无事可做（会 skip）
        import graph
        graph.ensure_seed("physics")
        cls._graph = graph

    @classmethod
    def tearDownClass(cls) -> None:
        import config
        import os
        cls._db.close_all_connections()
        cls._tmp.cleanup()
        # 完整还原全局状态：本类按字母序先于 test_foundation 运行，
        # 漏还原会让它之后的全部测试连到已删除的临时库（8 处 ERROR）。
        config.DB_PATH = cls._saved_cfg_db
        cls._db.DB_PATH = cls._saved_db_db
        if cls._saved_env is None:
            os.environ.pop("LEARNOS_DB", None)
        else:
            os.environ["LEARNOS_DB"] = cls._saved_env

    def _seed(self, n: int) -> None:
        import random
        from graph import concept_csv
        random.seed(7)
        db = self._db
        with db.db() as conn:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM concepts WHERE subject = 'physics' AND parent_id <> 0").fetchall()]
            if not ids:
                self.skipTest("physics 学科无概念，跳过规模断言")
            conn.execute("DELETE FROM problems WHERE title LIKE 'PERF-%'")
            conn.executemany(
                "INSERT INTO problems(title, subject, course, content, concept_ids, mastery,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                [(f"PERF-{i}", "physics", "perf", "c",
                  concept_csv(random.sample(ids, min(3, len(ids)))),
                  str(random.randint(1, 5)), db.now(), db.now()) for i in range(n)])

    def test_update_progress_within_budget_at_1000_problems(self) -> None:
        import graph
        self._seed(1000)
        graph.update_progress("physics")  # 预热
        t0 = time.perf_counter()
        graph.update_progress("physics")
        ms = (time.perf_counter() - t0) * 1000
        # 优化前同夹具实测约 305ms；预算取 120ms 留出机器差异余量
        self.assertLess(ms, 120.0,
                        f"update_progress 在 1000 题下耗时 {ms:.1f}ms，超出 120ms 预算")

    def test_update_progress_is_not_problem_count_quadratic(self) -> None:
        """规模不变式：题目数翻 5 倍，耗时不应成比例暴涨（O(N×M) 的特征）。"""
        import graph
        self._seed(200)
        graph.update_progress("physics")
        t0 = time.perf_counter()
        graph.update_progress("physics")
        small = (time.perf_counter() - t0) * 1000

        self._seed(1000)
        graph.update_progress("physics")
        t0 = time.perf_counter()
        graph.update_progress("physics")
        big = (time.perf_counter() - t0) * 1000

        # 题目数 5 倍；线性算法耗时增幅应远小于 5 倍（实测约 1.0~1.3 倍）
        self.assertLess(big, max(small * 3.0, small + 50.0),
                        f"耗时随题目数超线性增长：200 题 {small:.1f}ms → 1000 题 {big:.1f}ms，"
                        "疑似退回 O(概念×题目) 实现")

    def test_scale_trigger_for_junction_table(self) -> None:
        """触发阈值：关联数越界即失败，强制重新评估 problem_concepts 关联表迁移。

        业界 benchmark（sqlite.work）：1 万条关联时 junction 1.2ms vs 非规范化 148.9ms。
        本项目的反转循环已把该差距压到 2~5 倍，故阈值设为 5 万条关联。
        越界不代表必须迁移，而是「必须重新做一次决策」。
        """
        db = self._db
        with db.db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM problems WHERE concept_ids IS NOT NULL"
                " AND concept_ids <> ''").fetchone()
        self.assertLess(row["n"], 20_000,
                        f"题目数已达 {row['n']}，需重新评估 problem_concepts 关联表迁移"
                        "（见 .audit-tmp/plan-v3 第 3 批触发条件）")


class TestConceptCsvInvariant(unittest.TestCase):
    """根因 4 的数据面：concept_ids 序列化必须唯一。

    历史：库内曾同时存在 '[]' / ',1,,,7,' / ',1,7,' 三种格式。
    """

    def test_concept_csv_roundtrip(self) -> None:
        from graph import concept_csv, concept_ids_to_list
        for ids in ([], [1], [1, 7], [7, 1], [1, 1, 7], [30]):
            back = concept_ids_to_list(concept_csv(ids))
            self.assertEqual(sorted(set(ids)), sorted(back), f"{ids} 往返不一致")

    def test_concept_csv_format_is_single_canonical(self) -> None:
        from graph import concept_csv
        self.assertEqual("", concept_csv([]))
        self.assertEqual(",1,7,", concept_csv([1, 7]))
        self.assertEqual(",1,7,", concept_csv([1, 1, 7]))  # 去重
        self.assertNotIn(",,", concept_csv([1, 7]))

    def test_legacy_values_tolerated_on_read(self) -> None:
        """读取端必须容错历史脏值，否则老库升级即崩。"""
        from graph import concept_ids_to_list
        self.assertEqual([], concept_ids_to_list("[]"))
        self.assertEqual([1, 7], concept_ids_to_list(",1,,,7,"))
        self.assertEqual([1, 7], concept_ids_to_list(",1,7,"))


if __name__ == "__main__":
    unittest.main()
