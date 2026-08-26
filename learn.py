"""学习台后端：教材注册表（materials）+ 内容读取 + HTML 消毒 + md 渲染 + 全文搜索。

P0 范围（learning-workbench-plan.md）：
- materials 表 CRUD（v28 迁移，见 db.py）
- md/txt/html → 安全 HTML（服务端白名单消毒，前端直接注入 + KaTeX 渲染）
- pdf → 原样字节流交给前端查看器（不参与 AI 提取闭环）
- search_materials：零依赖 LIKE 检索（升级路径：接入 rag.ingest 后走 BM25）

安全不变量：
- sanitize_html 是唯一放行外部 HTML 的通道：白名单标签/属性、剥 script/style/iframe
  及一切 on* 事件属性、javascript:/vbscript: URL 一律拒绝。
- 文件路径一律工作区相对路径，经 rag._safe_relative 校验，杜绝越界读写。
"""
from __future__ import annotations

import json
import re
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from config import LOG
from db import DB_LOCK, db, now, row, rows

# ── HTML 白名单消毒 ────────────────────────────────────────────────────────

_ALLOWED_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "table", "thead", "tbody", "tr", "td", "th", "caption",
    "code", "pre", "strong", "em", "b", "i", "u", "s", "sup", "sub",
    "blockquote", "br", "hr", "a", "img", "span", "div", "figure", "figcaption",
}
# 进入即丢弃整块内容（含子树）的危险标签
_DROP_CONTENT = {"script", "style", "iframe", "object", "embed", "svg", "math",
                 "form", "template", "noscript"}
_VOID_TAGS = {"br", "hr", "img"}
# 各标签允许的属性（其余一律剥除；on* 事件属性全灭）
_ALLOWED_ATTRS: dict[str, set[str]] = {
    "a": {"href", "title"},
    "img": {"src", "alt", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}

_URL_RE = re.compile(r"^(https?:)?//|^/", re.IGNORECASE)
_DATA_IMG_RE = re.compile(r"^data:image/(png|jpe?g|gif|webp|svg\+xml);base64,", re.IGNORECASE)
_BAD_SCHEME_RE = re.compile(r"^\s*(javascript|vbscript|file|about):", re.IGNORECASE)


def _safe_url(url: str, is_img: bool = False) -> str | None:
    url = (url or "").strip()
    if not url or _BAD_SCHEME_RE.match(url):
        return None
    if _URL_RE.match(url):
        return url
    if is_img and _DATA_IMG_RE.match(url):
        return url
    # 相对路径（无 scheme）：放行（页面同源，且路径由本站生成/上传白名单控制）
    if ":" not in url.split("/", 1)[0]:
        return url
    return None


class _Sanitizer(HTMLParser):
    """白名单重建式消毒器：只输出允许的标签/属性，文本节点一律转义。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._drop_depth = 0          # >0 表示正处于被丢弃内容的子树内
        self._open: list[str] = []    # 已输出的未闭合标签栈（EOF 兜底闭合）

    # -- 属性重建 --

    def _rebuild_attrs(self, tag: str, attrs: list[tuple[str, Any | None]]) -> str:
        allowed = _ALLOWED_ATTRS.get(tag, set())
        parts: list[str] = []
        for name, value in attrs:
            name = (name or "").lower()
            if name.startswith("on") or name not in allowed:
                continue
            val = str(value if value is not None else "")
            if name in ("href", "src"):
                val = _safe_url(val, is_img=(tag == "img"))
                if val is None:
                    continue
            parts.append(f'{name}="{escape(val, quote=True)}"')
        if tag == "a":
            parts.append('rel="noopener nofollow" target="_blank"')
        return (" " + " ".join(parts)) if parts else ""

    # -- 解析回调 --

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Any | None]]) -> None:
        tag = tag.lower()
        if self._drop_depth:
            if tag in _DROP_CONTENT:
                self._drop_depth += 1
            return
        if tag in _DROP_CONTENT:
            self._drop_depth = 1
            return
        if tag not in _ALLOWED_TAGS:
            return  # 未知标签：丢弃标签本身，保留其文本内容
        attr_s = self._rebuild_attrs(tag, attrs)
        if tag in _VOID_TAGS:
            self.out.append(f"<{tag}{attr_s}>")
        else:
            self.out.append(f"<{tag}{attr_s}>")
            self._open.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Any | None]]) -> None:
        tag = tag.lower()
        if self._drop_depth or tag not in _ALLOWED_TAGS:
            return
        attr_s = self._rebuild_attrs(tag, attrs)
        self.out.append(f"<{tag}{attr_s} />")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._drop_depth:
            if tag in _DROP_CONTENT:
                self._drop_depth -= 1
            return
        if tag in _ALLOWED_TAGS and tag not in _VOID_TAGS and tag in self._open:
            # 弹栈到匹配标签为止（容忍上游错位嵌套，保证输出配平）
            while self._open:
                top = self._open.pop()
                self.out.append(f"</{top}>")
                if top == tag:
                    break

    def handle_data(self, data: str) -> None:
        if not self._drop_depth:
            self.out.append(escape(data, quote=False))

    def close(self) -> None:
        super().close()
        while self._open:  # EOF 兜底：闭合所有残留标签
            self.out.append(f"</{self._open.pop()}>")

    def result(self) -> str:
        return "".join(self.out)


def sanitize_html(raw: str) -> str:
    """外部 HTML → 白名单安全 HTML。导入的 .html 教材必须经此通道才能进渲染区。"""
    p = _Sanitizer()
    try:
        p.feed(str(raw or ""))
        p.close()
    except Exception as exc:  # 畸形输入：宁可不渲染也不放行
        LOG.warning("HTML 消毒失败，按纯文本降级: %s", exc)
        return f"<p>{escape(str(raw or ''), quote=False)}</p>"
    return p.result()


# ── Markdown → 安全 HTML（零依赖最小实现）─────────────────────────────────

_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_WIKI_RE = re.compile(r"\[\[([^\[\]\n]{1,40})\]\]")
_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*|__([^_\n]+)__")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)|(?<!_)_([^_\n]+)_(?!_)")
_STRIKE_RE = re.compile(r"~~([^~\n]+)~~")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_HR_RE = re.compile(r"^-{3,}$|^\*{3,}$|^_{3,}$")
_UL_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_OL_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")


def _safe_href(url: str, is_img: bool = False) -> str | None:
    return _safe_url(url, is_img=is_img)


def _inline(text: str, wiki: dict[str, str] | None = None) -> str:
    """行内格式：先整体转义，再按占位符保护代码段，最后恢复。

    wiki：概念名 → 跳转 URL 的映射（[[双链]]）。None 时 [[x]] 原样保留；
    给出但未命中 → 渲染为虚线 missing span（待人工挂载，不自动猜别名）。
    """
    text = escape(text, quote=False)
    codes: list[str] = []

    def _stash(m: re.Match) -> str:
        codes.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = _INLINE_CODE_RE.sub(_stash, text)

    if wiki is not None:
        def _wiki(m: re.Match) -> str:
            name = m.group(1).strip()
            url = wiki.get(name)
            if url:
                return (f'<a class="wikilink" href="{escape(url, quote=True)}" '
                        f'title="跳转到图谱">{escape(name)}</a>')
            return (f'<span class="wikilink missing" '
                    f'title="未挂载到图谱（可在图谱中新建该概念）">{escape(name)}</span>')

        text = _WIKI_RE.sub(_wiki, text)

    def _img(m: re.Match) -> str:
        u = _safe_href(m.group(2), is_img=True)
        return f'<img src="{escape(u, quote=True)}" alt="{m.group(1)}">' if u else m.group(0)

    def _link(m: re.Match) -> str:
        u = _safe_href(m.group(2))
        inner = m.group(1)
        if u is None:
            return inner
        ext = ' rel="noopener nofollow" target="_blank"' if u.startswith(("http:", "https:")) else ""
        return f'<a href="{escape(u, quote=True)}"{ext}>{inner}</a>'

    text = _IMG_RE.sub(_img, text)
    text = _LINK_RE.sub(_link, text)
    text = _BOLD_RE.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", text)
    text = _ITALIC_RE.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", text)
    text = _STRIKE_RE.sub(lambda m: f"<s>{m.group(1)}</s>", text)
    # 恢复代码段（占位符本身来自转义后的原文，不会与正文冲突）
    text = re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)
    return text


def md_to_html(md_text: str, wiki: dict[str, str] | None = None) -> str:
    """零依赖 Markdown 子集渲染：标题/列表/引用/围栏代码/分隔线/行内格式。

    wiki：[[双链]] 解析表（概念名 → URL），由 read_content 按学科构建。
    输出全部经 escape/白名单生成，无任何原始透传，天然免消毒。
    """
    def inl(t: str) -> str:
        return _inline(t, wiki)

    lines = str(md_text or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    code_buf: list[str] = []
    in_code = False
    code_lang = ""
    list_tag: str | None = None   # 当前打开的 ul/ol
    quote_buf: list[str] = []

    def flush_para() -> None:
        nonlocal para
        if para:
            out.append("<p>" + "<br>".join(inl(ln) for ln in para) + "</p>")
            para = []

    def flush_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    def flush_quote() -> None:
        nonlocal quote_buf
        if quote_buf:
            out.append("<blockquote>" + "<br>".join(inl(ln) for ln in quote_buf) + "</blockquote>")
            quote_buf = []

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        # 围栏代码块 ``` 开合
        if stripped.startswith("```"):
            if in_code:
                out.append("<pre><code>" + escape("\n".join(code_buf), quote=False) + "</code></pre>")
                code_buf, in_code, code_lang = [], False, ""
            else:
                flush_para(); flush_list(); flush_quote()
                in_code, code_lang = True, stripped[3:].strip()
            continue
        if in_code:
            code_buf.append(raw_line)
            continue
        if not stripped:
            flush_para(); flush_list(); flush_quote()
            continue
        m = _HEADING_RE.match(stripped)
        if m:
            flush_para(); flush_list(); flush_quote()
            level = len(m.group(1))
            out.append(f"<h{level}>{inl(m.group(2).strip())}</h{level}>")
            continue
        if _HR_RE.match(stripped):
            flush_para(); flush_list(); flush_quote()
            out.append("<hr>")
            continue
        if stripped.startswith("&gt;") or stripped.startswith(">"):
            flush_para(); flush_list()
            quote_buf.append(stripped.lstrip(">").strip())
            continue
        m = _UL_RE.match(line)
        if m:
            flush_para(); flush_quote()
            if list_tag != "ul":
                flush_list()
                out.append("<ul>")
                list_tag = "ul"
            out.append(f"<li>{inl(m.group(1))}</li>")
            continue
        m = _OL_RE.match(line)
        if m:
            flush_para(); flush_quote()
            if list_tag != "ol":
                flush_list()
                out.append("<ol>")
                list_tag = "ol"
            out.append(f"<li>{inl(m.group(1))}</li>")
            continue
        flush_list(); flush_quote()
        para.append(line)
    # 收尾
    if in_code and code_buf:
        out.append("<pre><code>" + escape("\n".join(code_buf), quote=False) + "</code></pre>")
    flush_para(); flush_list(); flush_quote()
    return "\n".join(out)


def txt_to_html(text: str) -> str:
    """txt：整段转义 + 段落化，不做任何格式解析。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", str(text or "")) if p.strip()]
    return "\n".join(
        "<p>" + escape(p, quote=False).replace("\n", "<br>") + "</p>" for p in paras
    ) or "<p></p>"


# ── materials 注册表 CRUD ────────────────────────────────────────────────

_FMT_BY_SUFFIX = {".md": "md", ".markdown": "md", ".txt": "txt",
                  ".html": "html", ".htm": "html", ".pdf": "pdf"}
_TEXT_FMTS = ("md", "txt", "html")


def fmt_of_path(path: str) -> str | None:
    return _FMT_BY_SUFFIX.get(Path(path).suffix.lower())


def add_material(subject: str, title: str, path: str, source: str = "upload") -> int | None:
    """登记教材；path 需为工作区相对路径且扩展名受支持。重复 path 幂等返回已有 id。"""
    fmt = fmt_of_path(path)
    if not fmt:
        raise ValueError("仅支持 md / txt / html / pdf 文件")
    title = str(title or "").strip()[:80] or Path(path).stem[:80]
    with DB_LOCK, db() as conn:
        exist = conn.execute("SELECT id FROM materials WHERE path = ?", (path,)).fetchone()
        if exist:
            return int(exist["id"])
        cur = conn.execute(
            "INSERT INTO materials(subject, title, path, fmt, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (subject, title, path, fmt, source, now(), now()))
        return int(cur.lastrowid or 0) or None


def update_material(mid: int, subject: str | None = None, title: str | None = None) -> bool:
    sets, args = [], []
    if title is not None:
        t = str(title).strip()[:80]
        if not t:
            raise ValueError("标题不能为空")
        sets.append("title = ?"); args.append(t)
    if subject:
        sets.append("subject = ?"); args.append(subject)
    if not sets:
        return False
    sets.append("updated_at = ?"); args.append(now())
    args.append(mid)
    with DB_LOCK, db() as conn:
        cur = conn.execute(f"UPDATE materials SET {', '.join(sets)} WHERE id = ?", tuple(args))
        return cur.rowcount > 0


def delete_material(mid: int) -> bool:
    """删除注册记录（不动磁盘原文件）。"""
    with DB_LOCK, db() as conn:
        cur = conn.execute("DELETE FROM materials WHERE id = ?", (mid,))
        return cur.rowcount > 0


def get_material(mid: int) -> dict[str, Any] | None:
    r = row("SELECT * FROM materials WHERE id = ?", (mid,))
    return dict(r) if r else None


def list_materials(subject: str) -> list[dict[str, Any]]:
    return [dict(r) for r in rows(
        "SELECT * FROM materials WHERE subject = ? ORDER BY updated_at DESC, id DESC", (subject,))]


def _resolve_material_file(path: str) -> Path:
    fp = safe_relative(path)
    if not fp:
        raise ValueError("教材路径越界（必须位于工作区内）")
    if not fp.is_file():
        raise ValueError(f"教材文件不存在: {path}")
    return fp


def safe_relative(path: str) -> Path | None:
    """工作区内相对路径校验（本模块自带版本）。

    与 rag._safe_relative 语义一致，但 APP_DIR 取自调用时的 config.APP_DIR
    （动态读取），避免模块导入期绑定导致测试重绑/打包重定位失效。
    """
    from config import APP_DIR
    raw = str(path or "").strip()
    if not raw:
        return None
    fp = (APP_DIR / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    try:
        fp.relative_to(Path(APP_DIR).resolve())
    except ValueError:
        LOG.warning("学习台路径越界被拒: %s", raw)
        return None
    return fp


def _wiki_map(subject: str) -> dict[str, str]:
    """概念名 → 图谱跳转 URL（[[双链]] 精确匹配；不做别名猜测）。"""
    from urllib.parse import quote
    try:
        cs = rows("SELECT name FROM concepts WHERE subject = ?", (subject,))
    except Exception:
        return {}
    return {r["name"]: f"concept_map.html?subject={quote(subject)}&focus={quote(r['name'])}"
            for r in cs if r["name"]}


def read_content(mid: int) -> dict[str, Any]:
    """读取教材内容。文本类返回 html（已消毒）；pdf 返回文件 Path 由调用方发字节流。"""
    mat = get_material(mid)
    if not mat:
        raise ValueError("教材不存在")
    fp = _resolve_material_file(mat["path"])
    fmt = mat["fmt"]
    if fmt == "pdf":
        return {"material": mat, "fmt": fmt, "file": fp}
    raw = fp.read_bytes().decode("utf-8", errors="replace")
    if fmt == "md":
        html = md_to_html(raw, wiki=_wiki_map(mat["subject"]))
    elif fmt == "txt":
        html = txt_to_html(raw)
    else:
        html = sanitize_html(raw)
    return {"material": mat, "fmt": fmt, "html": html}


def save_authored(subject: str, title: str, content: str) -> tuple[int, str]:
    """自编教材：内容写 textbooks/<安全名>.md 并登记，返回 (id, 相对路径)。"""
    title = str(title or "").strip()[:80]
    if not title:
        raise ValueError("标题不能为空")
    safe = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fa5]", "_", title)[:60] or "untitled"
    from config import APP_DIR  # 函数内动态导入：测试/打包重绑 config.APP_DIR 后自动跟随
    base = APP_DIR / "textbooks"
    base.mkdir(exist_ok=True)
    target = base / f"{safe}.md"
    n = 2
    while target.exists():
        target = base / f"{safe}-{n}.md"
        n += 1
    target.write_text(str(content or ""), encoding="utf-8")
    rel = str(target.relative_to(APP_DIR)).replace("\\", "/")
    mid = add_material(subject, title, rel, source="authored")
    if mid is None:
        raise ValueError("教材登记失败")
    return mid, rel


# ── AI 整章教程草稿（P1 自编通道；草稿不落库，确认后走 save_authored）────

_GEN_SYSTEM = (
    "你是优秀的教材作者。按给定的教材标题与大纲，写一篇中文学习教程（Markdown 格式）。"
    "要求：① 严格按大纲分节，用 ## 二级标题作小节；② 先直觉引入再给定义，配一个具体例子；"
    "③ 指出常见误区；④ 结尾用列表给出 3 条要点小结。"
    "数学/物理公式用 LaTeX（$...$ 行内、$$...$$ 独立）。"
    "提到后续章节的概念时可用 [[概念名]] 双链。只输出正文，不要解释。"
)


def generate_chapter_draft(subject: str, title: str, outline: str) -> str:
    """AI 按大纲生成整章教程 Markdown 草稿。AI 未配置抛 ValueError（前端提示）。"""
    from ai import ai_configured, call_ai
    if not ai_configured():
        raise ValueError("AI 未配置：请在设置中填写 API Key 后使用整章生成")
    title = str(title or "").strip()[:80]
    outline = str(outline or "").strip()[:4000]
    if not title or not outline:
        raise ValueError("请提供教材标题和大纲要点")
    try:
        return str(call_ai(
            [{"role": "system", "content": _GEN_SYSTEM},
             {"role": "user", "content": f"【教材标题】{title}\n\n【大纲要点】\n{outline}"}],
            max_tokens=6000, tier="heavy", route="learn")).strip()
    except Exception as exc:
        LOG.warning("整章生成失败: %s", exc)
        raise ValueError(f"AI 调用失败：{exc}") from exc


# ── 批注（P0.5 划词四连 / P2 矢量图形）───────────────────────────────────

_ANNO_KINDS = ("highlight", "note", "shape")


def review_today(subject: str, k: int = 5) -> list[dict[str, Any]]:
    """今日回看：浮出旧批注（最久未建优先——类间隔重复的回看启发式）。

    只返回 highlight/note（shape 是视觉层无可读文本）；随教材标题/格式一并下发，
    供前端点击跳转。
    """
    out = []
    for r in rows("""
        SELECT a.id, a.kind, a.anchor, a.body, a.color, a.created_at,
               m.id AS material_id, m.title AS material_title, m.fmt AS material_fmt
        FROM annotations a
        JOIN materials m ON m.id = a.material_id
        WHERE m.subject = ? AND a.kind IN ('highlight', 'note')
        ORDER BY a.created_at ASC, a.id ASC LIMIT ?
    """, (subject, max(1, min(k, 20)))):
        d = dict(r)
        try:
            d["anchor"] = json.loads(d["anchor"])
        except (TypeError, ValueError):
            pass
        out.append(d)
    return out


def add_annotation(mid: int, kind: str, anchor: dict[str, Any],
                   body: str = "", color: str = "") -> int | None:
    """新增批注。

    - highlight/note：anchor 为 {prefix, quote, suffix} 三段锚（quote 必填非空）；
    - shape：anchor 为 {page}，body 为几何 JSON（type/x0/y0/x1/y1 或 points）。
    """
    if kind not in _ANNO_KINDS:
        raise ValueError(f"批注类型仅支持 {'/'.join(_ANNO_KINDS)}")
    if kind == "shape":
        page = (anchor or {}).get("page")
        if not isinstance(page, int) or page < 1:
            raise ValueError("图形批注缺少合法页码（anchor.page）")
        try:
            geo = json.loads(str(body or ""))
        except ValueError as exc:
            raise ValueError("图形批注 body 不是合法 JSON") from exc
        if not isinstance(geo, dict) or not geo.get("type"):
            raise ValueError("图形批注缺少几何类型（body.type）")
        clean_anchor = {"page": page}
    else:
        quote = str((anchor or {}).get("quote", "")).strip()
        if not quote:
            raise ValueError("批注缺少选中文本（anchor.quote）")
        clean_anchor = {
            "prefix": str((anchor or {}).get("prefix", ""))[:40],
            "quote": quote[:300],
            "suffix": str((anchor or {}).get("suffix", ""))[:40],
        }
    with DB_LOCK, db() as conn:
        if not conn.execute("SELECT id FROM materials WHERE id = ?", (mid,)).fetchone():
            return None
        cur = conn.execute(
            "INSERT INTO annotations(material_id, kind, anchor, body, color, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mid, kind, json.dumps(clean_anchor, ensure_ascii=False),
             str(body or "")[:2000], str(color or "")[:16], now()))
        return int(cur.lastrowid or 0) or None


def list_annotations(mid: int) -> list[dict[str, Any]]:
    out = []
    for r in rows("SELECT * FROM annotations WHERE material_id = ? ORDER BY id", (mid,)):
        d = dict(r)
        try:
            d["anchor"] = json.loads(d["anchor"])
        except (TypeError, ValueError):
            pass
        out.append(d)
    return out


def delete_annotation(aid: int) -> bool:
    with DB_LOCK, db() as conn:
        return conn.execute("DELETE FROM annotations WHERE id = ?", (aid,)).rowcount > 0


# ── 全文搜索（零依赖 LIKE；升级路径见模块 docstring）──────────────────────

_SNIPPET_CTX = 36


def _plain_text_of(fp: Path, fmt: str) -> str | None:
    try:
        if fmt == "pdf":
            from rag import _extract_pdf
            return "\n".join(_extract_pdf(fp))
        raw = fp.read_bytes().decode("utf-8", errors="replace")
        if fmt == "html":
            raw = re.sub(r"<[^>]+>", " ", sanitize_html(raw))
        return raw
    except Exception as exc:
        LOG.debug("搜索跳过不可读教材 %s: %s", fp.name, exc)
        return None


def search_materials(subject: str, q: str, k: int = 8) -> list[dict[str, Any]]:
    q = str(q or "").strip()
    if len(q) < 2:
        return []
    hits: list[dict[str, Any]] = []
    for mat in list_materials(subject):
        if len(hits) >= k:
            break
        fp = _resolve_material_file(mat["path"])
        text = _plain_text_of(fp, mat["fmt"])
        if not text:
            continue
        low_q, low_text = q.lower(), text.lower()
        pos, per_mat = 0, 0
        while per_mat < 3 and len(hits) < k:
            idx = low_text.find(low_q, pos)
            if idx < 0:
                break
            start = max(0, idx - _SNIPPET_CTX)
            end = min(len(text), idx + len(q) + _SNIPPET_CTX)
            snippet = text[start:end].replace("\n", " ").strip()
            hits.append({"material_id": mat["id"], "title": mat["title"],
                         "snippet": ("…" if start else "") + snippet + ("…" if end < len(text) else "")})
            per_mat += 1
            pos = idx + len(q)
    return hits
