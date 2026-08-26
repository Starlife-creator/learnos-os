"""LearnMixin — 学习台域 Handler 方法（P0：教材注册/内容/搜索/AI 问答）。

端点一览：
- GET  /api/learn/materials                 教材列表（当前学科）
- GET  /api/learn/materials/<id>/content    内容（文本→消毒 HTML；pdf→字节流）
- GET  /api/learn/search?q=                 教材全文检索
- POST /api/learn/materials                 登记（path 引用已上传文件 或 content 自编落盘）
- POST /api/learn/materials/<id>/update     改名 / 换学科
- POST /api/learn/ask                       AI 助手（可携带教材上下文）
- DELETE /api/learn/materials/<id>          删除注册（不动磁盘文件，写审计）

写端点全部经 do_POST/do_DELETE 的 _write_auth_ok() 闸门；AI 调用走 _ai_quota 配额。
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse, parse_qs

from config import LOG


class LearnMixin:
    # ── GET ──────────────────────────────────────────────────────────────

    def _handle_learn_materials(self) -> None:
        import learn
        self.json_response({"items": learn.list_materials(self.subject)})

    def _handle_learn_content(self, mid: int) -> None:
        import learn
        try:
            result = learn.read_content(mid)
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 404 if "不存在" in str(exc) else 400)
            return
        if result["fmt"] == "pdf":
            self._send_pdf(result["file"])
            return
        mat = result["material"]
        self.json_response({"id": mat["id"], "title": mat["title"], "fmt": mat["fmt"],
                            "content": result["html"]})

    def _send_pdf(self, fp) -> None:
        """PDF 字节流：Content-Length 必须准确（HTTP/1.1 keep-alive 定界要求）。"""
        try:
            body = fp.read_bytes()
        except OSError as exc:
            LOG.warning("教材 PDF 读取失败: %s", exc)
            self.json_response({"error": "文件读取失败"}, 500)
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass  # 客户端断开（良性）

    def _handle_learn_search(self) -> None:
        import learn
        qs = parse_qs(urlparse(self.path).query)
        q = qs.get("q", [""])[0]
        try:
            k = min(10, max(1, int(qs.get("k", ["8"])[0] or 8)))
        except ValueError:
            k = 8
        self.json_response({"items": learn.search_materials(self.subject, q, k)})

    # ── POST ─────────────────────────────────────────────────────────────

    def _handle_learn_add(self, data: dict[str, Any]) -> None:
        import learn
        subject = self._subject_of(data)
        path = str(data.get("path", "")).strip()
        title = str(data.get("title", "")).strip()
        content = data.get("content")
        try:
            if content is not None and not path:
                # 自编教材：内容直接落 textbooks/（source=authored）
                mid, rel = learn.save_authored(subject, title, str(content))
                LOG.info("自编教材登记: %s (%s)", rel, subject)
                self.json_response({"id": mid, "path": rel}, 201)
                return
            if not path:
                self.json_response({"error": "缺少教材路径或内容"}, 400)
                return
            fmt = learn.fmt_of_path(path)
            if not fmt:
                self.json_response({"error": "仅支持 md / txt / html / pdf 文件"}, 400)
                return
            fp = learn.safe_relative(path)
            if not fp or not fp.is_file():
                self.json_response({"error": f"工作区内不存在该文件: {path}"}, 400)
                return
            mid = learn.add_material(subject, title, path)
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 400)
            return
        self.json_response({"id": mid}, 201)

    def _handle_learn_update(self, mid: int, data: dict[str, Any]) -> None:
        import learn
        if not learn.get_material(mid):
            self.json_response({"error": "教材不存在"}, 404)
            return
        subject = self._subject_of(data) if data.get("subject") else None
        try:
            ok = learn.update_material(mid, subject=subject,
                                       title=data.get("title"))
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 400)
            return
        self.json_response({"ok": bool(ok)})

    def _handle_learn_review_today(self) -> None:
        import learn
        qs = parse_qs(urlparse(self.path).query)
        try:
            k = int(qs.get("k", ["5"])[0] or 5)
        except ValueError:
            k = 5
        self.json_response({"items": learn.review_today(self.subject, k)})

    def _handle_learn_annotations(self, mid: int) -> None:
        import learn
        if not learn.get_material(mid):
            self.json_response({"error": "教材不存在"}, 404)
            return
        self.json_response({"items": learn.list_annotations(mid)})

    def _handle_learn_anno_add(self, mid: int, data: dict[str, Any]) -> None:
        import learn
        try:
            aid = learn.add_annotation(
                mid, str(data.get("kind", "")), data.get("anchor") or {},
                body=str(data.get("body", "")), color=str(data.get("color", "")))
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 400)
            return
        if aid is None:
            self.json_response({"error": "教材不存在"}, 404)
            return
        self.json_response({"id": aid}, 201)

    def _handle_learn_generate(self, data: dict[str, Any]) -> None:
        """P1 自编通道：按标题+大纲生成整章教程 Markdown 草稿（不落库，前端确认后保存）。"""
        if not self._ai_quota("heavy"):
            return
        import learn
        try:
            draft = learn.generate_chapter_draft(
                self._subject_of(data),
                str(data.get("title", "")), str(data.get("outline", "")))
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 502 if "AI" in str(exc) else 400)
            return
        self.json_response({"draft": draft})

    def _handle_learn_apply_cards(self, data: dict[str, Any]) -> None:
        """划词原子卡确认入库：前端已展示草稿，此处只落库（草稿确认合规）。"""
        import material
        cards = data.get("cards")
        if not isinstance(cards, list) or not cards:
            self.json_response({"error": "没有可入库的卡片"}, 400)
            return
        clean = []
        for c in cards:
            if not isinstance(c, dict):
                continue
            q = str(c.get("question", "")).strip()
            a = str(c.get("answer", "")).strip()
            if q and a:
                clean.append({"question": q[:300], "answer": a[:2000],
                              "concept": str(c.get("concept", ""))[:40]})
        if not clean:
            self.json_response({"error": "卡片缺少问题或答案"}, 400)
            return
        added = material.apply_cards(clean, self._subject_of(data))
        LOG.info("学习台原子卡入库: %s 张 (%s)", added, self.subject)
        self.json_response({"added": added})

    def _handle_learn_ask(self, data: dict[str, Any]) -> None:
        """学习台 AI 助手：可选携带教材上下文。离线时明确报错不假装回答。"""
        if not self._ai_quota("fast"):
            return
        question = str(data.get("question", "")).strip()
        if not question:
            self.json_response({"error": "请输入问题"}, 400)
            return
        context = ""
        mid = data.get("material_id")
        if mid:
            import learn
            try:
                res = learn.read_content(int(mid))
            except ValueError as exc:
                self.json_response({"error": str(exc)}, 404)
                return
            if "html" in res:
                import re as _re
                context = _re.sub(r"<[^>]+>", " ", res["html"])
            elif "file" in res:
                # pdf：尽力取文本层（pdfminer 缺失则降级为无上下文）
                from learn import _plain_text_of
                context = _plain_text_of(res["file"], "pdf") or ""
            context = context.strip()[:8000]
        from ai import ai_configured, call_ai
        if not ai_configured():
            self.json_response({"error": "AI 未配置：请在设置中填写 API Key 后使用助手",
                                "offline": True}, 502)
            return
        system = ("你是学习台助手，基于给定的教材内容片段回答学习者的问题；"
                  "教材中没有的内容请注明「教材未涉及」。支持 LaTeX 公式（$...$）。")
        user = (f"【教材片段】\n{context}\n\n" if context else "") + f"【问题】{question}"
        try:
            answer = call_ai([{"role": "system", "content": system},
                              {"role": "user", "content": user}],
                             max_tokens=1200, tier="fast", route="learn")
        except Exception as exc:
            LOG.warning("学习台 AI 问答失败: %s", exc)
            self.json_response({"error": f"AI 调用失败：{exc}"}, 502)
            return
        self.json_response({"answer": str(answer).strip()})
