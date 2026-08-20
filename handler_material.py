"""MaterialMixin — 资料域 Handler 方法（material/rag/ocr/graph）。自 handler.py 原样迁移。"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse, parse_qs
from pathlib import Path

from config import LOG
from db import rows
from ai import get_cached_settings
from validate import SchemaError
import graph


class MaterialMixin:
    def _handle_material_analyze(self, data: dict[str, Any]) -> None:
        """资料导入向导：提取图谱/例题/试卷草稿（R3 不落库）。
        来源三选一：text（前端直传文本）、doc_id（RAG 已摄取文档）、path（工作区文件，含 uploads/）。
        Accept: text/event-stream 时走 SSE 进度流；支持 from_batch/max_batches 断点续跑。
        """
        if not self._ai_quota("heavy"):
            return  # R3：资料分析重接口（多轮 AI 调用），护额度
        import material
        text = str(data.get("text", "")).strip()
        doc_id = data.get("doc_id")
        path = str(data.get("path", "")).strip()
        try:
            if text:
                if len(text) > 6_000_000:
                    raise ValueError("文本过长（>600 万字符），请拆分文件")
            elif doc_id is not None:
                text = material.doc_text(int(doc_id))
            elif path:
                text = material.path_text(path)
            else:
                raise ValueError("请提供文本、RAG 文档或工作区文件路径")
            context_tokens = int(get_cached_settings().get("ai_context_tokens", "32000") or 32000)
            from_batch = int(data.get("from_batch", 0) or 0)
            max_batches = data.get("max_batches")
            max_batches = int(max_batches) if max_batches else None
            wants_sse = self._wants_sse()
            if wants_sse:
                self._stream_material_analyze(text, list(data.get("targets") or []),
                                              from_batch, max_batches, context_tokens)
                return
            result = material.analyze(text, self.subject, list(data.get("targets") or []),
                                      context_tokens=context_tokens, from_batch=from_batch,
                                      max_batches=max_batches)
        except (ValueError, SchemaError) as exc:
            self.json_response({"error": str(exc)}, 400)
            return
        except Exception as exc:  # AI 网络/接口异常 → 502，提示配置
            LOG.warning("资料提取失败: %s", exc)
            self.json_response({"error": f"提取失败：{exc}"}, 502)
            return
        self.json_response(result)

    def _handle_material_cards(self, data: dict[str, Any]) -> None:
        """§27 读书闭环基础版：粘贴文本 → 原子卡草稿；apply=true 则落库进 FSRS。

        来源同 analyze：text / doc_id / path。零依赖启发式始终可用，AI 可用时增强。
        """
        if not self._ai_quota("heavy"):
            return  # R3：原子卡生成重接口
        import material
        text = str(data.get("text", "")).strip()
        doc_id = data.get("doc_id")
        path = str(data.get("path", "")).strip()
        try:
            if text:
                if len(text) > 6_000_000:
                    raise ValueError("文本过长（>600 万字符），请拆分文件")
            elif doc_id is not None:
                text = material.doc_text(int(doc_id))
            elif path:
                text = material.path_text(path)
            else:
                raise ValueError("请提供文本、RAG 文档或工作区文件路径")
            use_ai = bool(data.get("use_ai", True))
            cards = material.extract_atomic_cards(text, self.subject, use_ai=use_ai)
            if data.get("apply"):
                added = material.apply_cards(cards, self.subject)
                self.json_response({"added": added, "cards": cards})
                return
            self.json_response({"cards": cards, "count": len(cards)})
        except (ValueError, SchemaError) as exc:
            self.json_response({"error": str(exc)}, 400)
        except Exception as exc:  # AI 网络/接口异常 → 502
            LOG.warning("原子卡生成失败: %s", exc)
            self.json_response({"error": f"生成失败：{exc}"}, 502)

    def _stream_material_analyze(self, text: str, targets: list[str], from_batch: int,
                                 max_batches: int | None, context_tokens: int) -> None:
        """SSE 分析流：start → progress* → done | error。每批 AI 调用完成即推送进度。"""
        import material
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            size = material.batch_chars(context_tokens)
            total_batches = len(material._refine_batch_boundaries(
                material._split_batches(text, size), size, dry_run=True))
            self._sse_send("start", {"batches_total": total_batches, "from_batch": from_batch})

            def on_progress(info: dict[str, Any]) -> None:
                self._sse_send("progress", info)

            result = material.analyze(text, self.subject, targets,
                                      context_tokens=context_tokens, from_batch=from_batch,
                                      max_batches=max_batches, progress=on_progress)
            self._sse_send("done", result)
        except Exception as exc:
            LOG.warning("资料提取流失败: %s", exc)
            self._sse_send("error", {"error": str(exc)})

    def _handle_material_upload(self) -> None:
        """资料上传落盘：POST /api/material/upload?name=<文件名>，原始字节流 → uploads/。"""
        import material
        qs = parse_qs(urlparse(self.path).query)
        name = (qs.get("name", [""])[0] or "upload.md").strip()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            rel = material.save_upload(name, self.rfile, length)
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 400)
            return
        LOG.info("资料上传: %s", rel)
        self.json_response({"path": rel}, 201)

    def _handle_material_apply(self, data: dict[str, Any]) -> None:
        """资料导入向导：用户确认后的草稿写入（图谱/题库/试卷）。"""
        import material
        payload = data.get("draft") or {}
        if not any(payload.get(k) for k in ("concepts", "questions", "paper")):
            self.json_response({"error": "没有可导入的内容"}, 400)
            return
        try:
            stats = material.apply_draft(payload, self.subject)
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 400)
            return
        LOG.info("资料导入完成 (%s): %s", self.subject, stats)
        self.json_response({"ok": True, "stats": stats})

    def _handle_ocr_probe(self) -> None:
        import ocr
        self.json_response({"ok": True, **ocr.probe()})

    def _handle_ocr_extract(self, data) -> None:
        """OCR 提取：工作区内 PDF/图片 → 文本。"""
        raw = str((data or {}).get("path", "")).strip()
        if not raw:
            self.json_response({"error": "缺少 path"}, 400)
            return
        from rag import _safe_relative
        fp = _safe_relative(raw)
        if not fp:
            self.json_response({"error": "路径必须在工作区内"}, 400)
            return
        if not fp.is_file():
            self.json_response({"error": f"文件不存在: {raw}"}, 400)
            return
        try:
            import ocr
            result = ocr.extract_pdf(fp) if fp.suffix.lower() == ".pdf" else ocr.extract_image(fp)
            result["path"] = str(fp)
            self.json_response({"ok": True, **result})
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 400)

    def _handle_rag_docs(self) -> None:
        import rag
        self.json_response({"items": rag.list_docs()})

    def _handle_rag_restore(self, doc_id: int) -> None:
        """撤销误删：恢复已删除的文档（内存快照）。"""
        import rag
        if not rag.restore_doc(doc_id):
            self.json_response({"error": "恢复失败（已过期或存在冲突）"}, 400)
            return
        self.json_response({"ok": True})

    def _handle_rag_ingest(self, data: dict[str, Any]) -> None:
        """B3：摄取工作区内教材/课件/笔记（文件或目录）。"""
        import rag
        path = str(data.get("path", "")).strip()
        if not path:
            self.json_response({"error": "请提供工作区内路径（文件或目录）"}, 400)
            return
        try:
            stats = rag.ingest_path(path)
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 400)
            return
        self.json_response({"ok": True, **stats})

    def _handle_rag_search(self) -> None:
        """B3：BM25（+可选 FTS5）检索，返回带溯源的结果。"""
        qs = parse_qs(urlparse(self.path).query)
        q = qs.get("q", [""])[0].strip()
        if not q:
            self.json_response({"items": [], "error": "缺少查询词"}, 400)
            return
        import rag
        k = min(10, max(1, int(qs.get("k", ["5"])[0] or 5)))
        items = rag.search(q, k)
        docs = {d["id"]: d for d in rag.list_docs()}
        for item in items:
            doc = docs.get(item["doc_id"])
            item["source_path"] = doc["source_path"] if doc else ""
            item["name"] = Path(doc["source_path"]).name if doc else ""
            item["total_chunks"] = doc["chunk_count"] if doc else 0
        self.json_response({"items": items})

    def _handle_rag_open(self) -> None:
        """B3：溯源跳转——打开已摄取文档的本地文件（仅限登记路径）。"""
        import rag
        qs = parse_qs(urlparse(self.path).query)
        path = qs.get("path", [""])[0]
        fp = rag._safe_relative(path)
        if not fp or str(fp) not in rag.registered_paths() or not fp.is_file():
            self.json_response({"error": "路径未登记或不存在"}, 400)
            return
        import os
        import webbrowser
        try:
            if os.name == "nt":
                os.startfile(str(fp))  # type: ignore[attr-defined]
            else:
                webbrowser.open(fp.as_uri())
        except OSError as exc:
            self.json_response({"error": f"打开文件失败: {exc}"}, 500)
            return
        self.json_response({"ok": True})

    def _handle_graph(self) -> None:
        """A2：指定学科全图谱，节点含掌握度，边含关系。"""
        subject = self.subject
        graph.update_progress_cached(subject)  # 图谱加载：TTL 护住重复调用
        data = graph.load_graph(subject)
        # 附加绑定题数（looms_in）与实际掌握度：按学科过滤（concept_progress 全学科混存）
        prog_rows = rows("""
            SELECT cp.concept_id AS cid, COALESCE(cp.reviews, 0) AS bound,
                   COALESCE(cp.mastery, 0.0) AS m
            FROM concept_progress cp
            JOIN concepts c ON c.id = cp.concept_id
            WHERE c.subject = ?
        """, (subject,))
        bound_map = {int(p["cid"]): int(p["bound"]) for p in prog_rows}
        mastery_map = {int(p["cid"]): float(p["m"]) for p in prog_rows}
        for node in data["nodes"]:
            nid = int(node["id"])
            node["looms_in"] = bound_map.get(nid, 0)
            node["mastery_est"] = round(mastery_map.get(nid, 0.0), 3)
        self.json_response(data)

    def _handle_graph_problems(self) -> None:
        """A2 先修模式：?concept=<id> 返回该概念先修链上的相关错题。"""
        qs = parse_qs(urlparse(self.path).query)
        cid = qs.get("concept", [""])[0]
        if not cid.isdigit():
            self.json_response({"error": "缺少 concept 参数"}, 400)
            return
        chain = graph.prereq_chain(int(cid))
        items = graph.problems_for_concepts(chain)
        self.json_response({"items": items, "chain_count": len(chain)})

    def _handle_graph_add(self, data: dict[str, Any]) -> None:
        """A2：用户新增概念（user_edited=1，改动不写回 seed）。"""
        subject = self.subject
        cid = graph.add_concept(str(data.get("name", "")), int(data.get("parent_id", 0) or 0),
                                subject=subject)
        if not cid:
            self.json_response({"error": "概念名不能为空或已存在"}, 400)
            return
        aliases = str(data.get("aliases", "")).strip()
        if aliases:
            graph.update_aliases(cid, aliases)
        graph.update_progress(subject, force=True)
        self.json_response({"id": cid})

    def _handle_graph_unlinked(self) -> None:
        """未链接提及：错题文本中出现但未绑定的概念（含别名），建议绑定。"""
        self.json_response({"items": graph.unlinked_mentions(self.subject)})

    def _handle_graph_bind(self, data: dict[str, Any]) -> None:
        """确认绑定概念到错题（未链接提及的一键确认）。"""
        try:
            pid = int(data.get("problem_id", 0))
            cid = int(data.get("concept_id", 0))
        except (TypeError, ValueError):
            self.json_response({"error": "参数不合法"}, 400)
            return
        if not graph.bind_concept(pid, cid, self.subject):
            self.json_response({"error": "题目或概念不存在"}, 404)
            return
        self.json_response({"ok": True})
