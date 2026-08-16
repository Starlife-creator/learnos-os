"""HTTP 请求处理器：路由分发与 JSON 响应。"""
from __future__ import annotations

import base64
import csv
import io
import json
import re
import shutil
import time
import uuid
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler
from typing import Any
from urllib.parse import urlparse, parse_qs
from pathlib import Path

from config import STATIC_DIR, LOG, DB_PATH, MEDIA_DIR
from db import DB_LOCK, db, now, row, rows, settings_dict, subject_exists, list_subjects
from ai import (
    call_ai, call_ai_stream, fallback_hint, problem_prompt, extract_tags, generate_variants,
    invalidate_settings_cache, set_runtime_key, set_master_password,
    display_settings, get_cached_settings,
)
from review import compute_review, clamp_mastery
from oral import (
    start_oral, continue_oral, draft_oral_card, start_feynman,
    feynman_self_review, save_feynman_self_review,
)
import graph
from errors import normalize_error_type, ERROR_TYPE_LABELS, is_valid_error_type
from validate import SchemaError
from handler_material import MaterialMixin
from handler_problems import ProblemsMixin
from handler_reviews import ReviewsMixin
from handler_reports import ReportsMixin
from handler_oral import OralMixin
import fsrs_bridge
from fsrs_bridge import next_interval_days
from handler_base import (X_HEADER, X_VALUE, _IDEMPOTENCY, _IDEMPOTENCY_TTL,
                          _as_str_list, _interleave, _prune_idempotency)


class Handler(MaterialMixin, OralMixin, ProblemsMixin, ReviewsMixin,
             ReportsMixin, SimpleHTTPRequestHandler):
    server_version = "LearnOS/0.5.0"

    # 路由表：(正则模式, 处理方法名, 是否需要请求体)。路径数字组自动转 int 传入。
    # 保持声明顺序：互斥 fullmatch，先声明先命中。
    GET_ROUTES: list[tuple[str, str]] = [
        (r"/api/search", "_handle_global_search"),
        (r"/api/fsrs/optimal", "_handle_fsrs_optimal"),
        (r"/api/dashboard", "_handle_dashboard"),
        (r"/api/problems", "_handle_list_problems"),
        (r"/api/problems/(\d+)/history", "_handle_problem_history"),
        (r"/api/problems/(\d+)/related", "_handle_related_problems"),
        (r"/api/problems/duplicates", "_handle_duplicates"),
        (r"/api/problems/(\d+)", "_handle_get_problem"),
        (r"/api/fsrs/train", "_handle_fsrs_train"),
        (r"/api/fsrs/reset", "_handle_fsrs_reset"),
        (r"/api/fsrs/status", "_handle_fsrs_status"),
        (r"/api/gamification", "_handle_gamification"),
        (r"/api/report/weekly", "_handle_weekly_report"),
        (r"/api/report/monthly", "_handle_monthly_report"),
        (r"/api/reviews", "_handle_list_reviews"),
        (r"/api/reviews/summary/today", "_handle_today_summary"),
        (r"/api/settings", "_handle_settings"),
        (r"/api/trend", "_handle_trend"),
        (r"/api/analytics", "_handle_analytics"),
        (r"/api/profile", "_handle_profile"),
        (r"/api/graph/concepts", "_handle_graph"),
        (r"/api/graph/problems", "_handle_graph_problems"),
        (r"/api/graph/unlinked", "_handle_graph_unlinked"),
        (r"/api/feynman/(\d+)/self-review", "_handle_feynman_self_review_get"),
        (r"/api/oral/(\d+)", "_handle_get_oral"),
        (r"/api/export", "_handle_export"),
        (r"/api/export/backup", "_handle_backup_export"),
        (r"/api/ocr/probe", "_handle_ocr_probe"),
        (r"/api/health", "_handle_health"),
        (r"/api/models/probe", "_handle_models_probe"),
        (r"/api/rag/docs", "_handle_rag_docs"),
        (r"/api/rag/search", "_handle_rag_search"),
        (r"/api/rag/open", "_handle_rag_open"),
        (r"/api/exam/papers", "_handle_exam_papers"),
        (r"/api/exam/papers/(\d+)", "_handle_exam_paper"),
        (r"/api/bank", "_handle_bank"),
        (r"/api/bank/units", "_handle_bank_units"),
        (r"/api/bank/stats", "_handle_bank_stats"),
        (r"/api/subjects", "_handle_subjects"),
    ]

    POST_ROUTES: list[tuple[str, str, bool]] = [
        (r"/api/problems", "_handle_create_problem", True),
        (r"/api/problems/(\d+)/hint", "_handle_hint", True),
        (r"/api/problems/(\d+)/variants/generate", "_handle_generate_variants", False),
        (r"/api/problems/(\d+)/variants", "_handle_save_variants", True),
        (r"/api/problems/batch", "_handle_batch", True),
        (r"/api/reviews/(\d+)/complete", "_handle_complete_review", True),
        (r"/api/oral/(\d+)/end", "_handle_oral_end", False),
        (r"/api/oral/(\d+)/draft-card", "_handle_oral_draft_card", False),
        (r"/api/oral/start", "_handle_oral_start", True),
        (r"/api/oral/respond", "_handle_oral_respond", True),
        (r"/api/feynman/(\d+)/self-review", "_handle_feynman_self_review", True),
        (r"/api/feynman/start", "_handle_feynman_start", True),
        (r"/api/upload/photo", "_handle_upload_photo", True),
        (r"/api/ai/extract-photo", "_handle_extract_photo", True),
        (r"/api/ai/extract-tags", "_handle_extract_tags", True),
        (r"/api/rag/ingest", "_handle_rag_ingest", True),
        (r"/api/rag/doc/(\d+)/restore", "_handle_rag_restore", False),
        (r"/api/exam/papers/(\d+)/questions", "_handle_exam_add_questions", True),
        (r"/api/exam/papers", "_handle_exam_create", True),
        (r"/api/graph/concepts", "_handle_graph_add", True),
        (r"/api/graph/bind", "_handle_graph_bind", True),
        (r"/api/ocr/extract", "_handle_ocr_extract", True),
        (r"/api/import", "_handle_import", True),
        (r"/api/import/restore", "_handle_backup_restore", True),
        (r"/api/settings/test", "_handle_settings_test", True),
        (r"/api/fsrs/train", "_handle_fsrs_train", False),
        (r"/api/fsrs/retention", "_handle_fsrs_retention", True),
        (r"/api/keystore/unlock", "_handle_keystore_unlock", True),
        (r"/api/keystore/clear", "_handle_keystore_clear", True),
        (r"/api/bank/attempt", "_handle_bank_attempt", True),
        (r"/api/bank/import", "_handle_bank_import", True),
        (r"/api/subjects", "_handle_add_subject", True),
        (r"/api/material/analyze", "_handle_material_analyze", True),
        (r"/api/material/apply", "_handle_material_apply", True),
    ]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def end_headers(self) -> None:
        # CSP：禁外部脚本/样式/连接；script-src 收紧为仅同源（无内联 <script>），
        # script-src-attr 放行既有内联事件属性（onclick 等，函数调用白名单，无法注入整块代码）
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "script-src-attr 'unsafe-inline'; connect-src 'self'",
        )
        super().end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def _csrf_ok(self) -> bool:
        return self.headers.get(X_HEADER) == X_VALUE

    def json_response(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            # 客户端已断开连接（浏览器取消请求 / 关标签页）：无处可写，直接放弃
            pass

    def read_json(self, max_bytes: int = 1_000_000) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > max_bytes:
            raise ValueError("请求内容过大")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _safe_error(self, exc: Exception, status: int = 500) -> None:
        """返回通用错误消息，不暴露内部细节。"""
        if isinstance(exc, OSError):
            # 连接层错误（WinError 10053/10054 等）多为客户端主动断开，属良性，不刷屏
            LOG.debug("连接中断（忽略）: %s", exc)
            self.json_response({"error": "服务器内部错误，请查看日志"}, status)
            return
        if isinstance(exc, (ValueError, json.JSONDecodeError)):
            self.json_response({"error": str(exc)}, 400)
        else:
            LOG.error("请求处理异常: %s", exc, exc_info=True)
            self.json_response({"error": "服务器内部错误，请查看日志"}, status)

    # ── GET ──────────────────────────────────────────────

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        self.subject = self._subject_from_qs()
        try:
            for pattern, method in Handler.GET_ROUTES:
                match = re.fullmatch(pattern, path)
                if not match:
                    continue
                args = tuple(int(g) for g in match.groups())
                getattr(self, method)(*args)
                return
            if path.startswith("/media/"):
                self._serve_media(path)
                return
            super().do_GET()
        except Exception as exc:
            self._safe_error(exc)

    # ── 学科上下文（多学科；注册表驱动，网页端可增删）──

    _SUBJECT_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,19}")

    def _subject_from_qs(self) -> str:
        qs = parse_qs(urlparse(self.path).query)
        return self._valid_subject(qs.get("subject", [""])[0])

    def _valid_subject(self, raw: str) -> str:
        raw = str(raw or "").strip()
        if raw and subject_exists(raw):
            return raw
        return "physics"

    def _subject_of(self, data: dict[str, Any]) -> str:
        return self._valid_subject(str(data.get("subject", "")))

    # ── GET 轻量端点（原内联分支，统一为方法以便路由表分发）──

    def _handle_fsrs_reset(self) -> None:
        self.json_response({"ok": fsrs_bridge.reset_parameters()})

    def _handle_fsrs_status(self) -> None:
        self.json_response(fsrs_bridge.fsrs_status())

    def _handle_settings(self) -> None:
        self.json_response(display_settings())

    def _handle_profile(self) -> None:
        from profile import aggregate
        self.json_response(aggregate())

    def _handle_health(self) -> None:
        self.json_response({"ok": True, "version": "0.5.0"})

    def _handle_models_probe(self) -> None:
        from ai import probe_ollama
        self.json_response({"ollama": probe_ollama()})

    def _handle_exam_papers(self) -> None:
        import exam
        self.json_response(exam.overall_readiness())

    def _handle_exam_paper(self, paper_id: int) -> None:
        import exam
        data = exam.paper_readiness(paper_id)
        if not data:
            self.json_response({"error": "试卷不存在"}, 404)
            return
        self.json_response(data)

    def _handle_bank(self) -> None:
        import bank
        qs = parse_qs(urlparse(self.path).query)
        subject = self.subject
        self.json_response({
            "items": bank.list_questions(
                unit=qs.get("unit", [""])[0],
                status=qs.get("status", ["all"])[0],
                q=qs.get("q", [""])[0],
                subject=subject,
            ),
            "stats": bank.stats(subject),
        })

    def _handle_bank_units(self) -> None:
        import bank
        self.json_response({"units": bank.units(self.subject)})

    def _handle_bank_stats(self) -> None:
        import bank
        self.json_response(bank.stats(self.subject))

    def _handle_subjects(self) -> None:
        """学科列表：注册表（内置三科 + 种子学科 + 网页端自建）。"""
        subjects = list_subjects()
        for s in subjects:
            s.setdefault("title", s["id"])
            if not s["title"]:
                s["title"] = s["id"]
        self.json_response({"subjects": subjects, "current": self.subject})

    def _handle_add_subject(self, data: dict[str, Any]) -> None:
        """网页端新增学科：合法 id + 可选标题；有种子文件则自动加载图谱。"""
        sid = str(data.get("id", "")).strip()
        title = str(data.get("title", "")).strip() or sid
        if not self._SUBJECT_ID_RE.fullmatch(sid):
            self.json_response({"error": "学科 id 需字母开头，仅限字母/数字/下划线，最长 20 字符"}, 400)
            return
        if subject_exists(sid):
            self.json_response({"error": f"学科 {sid} 已存在"}, 409)
            return
        with DB_LOCK, db() as conn:
            conn.execute(
                "INSERT INTO subjects(id, title, builtin, created_at) VALUES (?, ?, 0, ?)",
                (sid, title, now()),
            )
        import graph
        graph.ensure_seed(sid)
        LOG.info("新增学科: %s (%s)", sid, title)
        self.json_response({"ok": True, "subject": {"id": sid, "title": title, "builtin": False}}, 201)

    def _handle_delete_subject(self, subject_id: str) -> None:
        """删除自建学科：内置不可删；已有数据（错题/概念/题库作答）时阻止。"""
        info = row("SELECT id, builtin FROM subjects WHERE id = ?", (subject_id,))
        if not info:
            self.json_response({"error": "学科不存在"}, 404)
            return
        if info["builtin"]:
            self.json_response({"error": "内置学科不可删除"}, 400)
            return
        counts: dict[str, int] = {}
        for name, sql in (
            ("problems", "SELECT COUNT(*) AS c FROM problems WHERE subject = ?"),
            ("concepts", "SELECT COUNT(*) AS c FROM concepts WHERE subject = ?"),
            ("bank_problems", "SELECT COUNT(*) AS c FROM bank_problems WHERE subject = ?"),
        ):
            counts[name] = int(row(sql, (subject_id,))["c"])
        if any(counts.values()):
            detail = "、".join(f"{k} {v}" for k, v in counts.items() if v)
            self.json_response({"error": f"该学科仍有数据（{detail}），请先清空后再删除"}, 409)
            return
        with DB_LOCK, db() as conn:
            conn.execute("DELETE FROM subjects WHERE id = ?", (subject_id,))
        LOG.info("删除学科: %s", subject_id)
        self.json_response({"ok": True})

    def _handle_global_search(self) -> None:
        """全局搜索（Ctrl+K）：跨错题/概念/题库/RAG 文档，各组最多 6 条。"""
        qs = parse_qs(urlparse(self.path).query)
        q = (qs.get("q", [""])[0] or "").strip()
        if len(q) < 1:
            self.json_response({"problems": [], "concepts": [], "bank": [], "docs": []})
            return
        like = f"%{q}%"
        problems = rows(
            "SELECT id, title, topic, mastery FROM problems "
            "WHERE subject = ? AND (title LIKE ? OR content LIKE ? OR topic LIKE ?) "
            "ORDER BY updated_at DESC LIMIT 6",
            (self.subject, like, like, like))
        concepts = rows(
            "SELECT id, name FROM concepts WHERE subject = ? AND name LIKE ? "
            "ORDER BY mastery_est ASC LIMIT 6", (self.subject, like))
        bank_hits: list[dict[str, Any]] = []
        try:
            import bank
            for question in bank.load_bank(self.subject).get("questions", []):
                stem = str(question.get("stem", ""))
                if q.lower() in stem.lower() or q.lower() in str(question.get("concept", "")).lower():
                    bank_hits.append({"id": question.get("id", ""), "stem": stem[:80],
                                      "concept": question.get("concept", "")})
                if len(bank_hits) >= 6:
                    break
        except Exception:
            pass
        docs: list[dict[str, Any]] = []
        try:
            import rag
            for hit in rag.search(q, k=3):
                docs.append({"name": hit.get("name", ""), "path": hit.get("source_path", ""),
                             "page": hit.get("page", 0)})
        except Exception:
            pass
        self.json_response({"problems": problems, "concepts": concepts,
                            "bank": bank_hits, "docs": docs})

    def _handle_fsrs_train(self) -> None:
        """P0：后台训练个性化 FSRS 参数（POST 立即返回，状态走 /api/fsrs/status）。"""
        reviews = rows("""
            SELECT problem_id AS cid, result AS rating, created_at AS ts
            FROM reviews WHERE completed = 1 AND result BETWEEN 1 AND 4
            ORDER BY id
        """)
        sample = [(int(r["cid"]), int(r["rating"]), str(r["ts"])) for r in reviews]
        if not fsrs_bridge.fsrs_available():
            self.json_response({"started": False, "error": "FSRS 未启用（vendor 缺失）"}, 409)
            return
        if len(sample) < 10:
            self.json_response({"started": False, "error": f"复习记录不足（需 ≥10 条，当前 {len(sample)} 条）"}, 409)
            return
        started = fsrs_bridge.train_async(sample)
        self.json_response({"started": started, "sample_count": len(sample)})

    def do_POST(self) -> None:
        if not self._csrf_ok():
            self.json_response({"error": "请求来源不被信任 (缺少 X-Requested-With)"}, 403)
            return
        path = urlparse(self.path).path
        try:
            # 资料上传走原始字节流（在 JSON 解析前处理，避免整体读入内存）
            if path == "/api/material/upload":
                self.subject = self._subject_from_qs()
                self._handle_material_upload()
                return
            # 资料导入直传文本可到 8MB（与拍照上传一致），其余接口维持 1MB
            limit = 8_000_000 if path == "/api/material/analyze" else 1_000_000
            data = self.read_json(max_bytes=limit)
            self.subject = self._subject_from_qs()
            if isinstance(data, dict) and data.get("subject"):
                self.subject = self._valid_subject(str(data["subject"]))
            for pattern, method, needs_data in Handler.POST_ROUTES:
                match = re.fullmatch(pattern, path)
                if not match:
                    continue
                args = tuple(int(g) for g in match.groups())
                if needs_data:
                    getattr(self, method)(*args, data)
                else:
                    getattr(self, method)(*args)
                return
            self.json_response({"error": "接口不存在"}, 404)
        except Exception as exc:
            self._safe_error(exc)

    # ── POST 轻量端点（原内联分支，统一为方法以便路由表分发）──

    def _handle_bank_attempt(self, data: dict[str, Any]) -> None:
        try:
            import bank
            result = bank.judge(str(data.get("qid", "")), data.get("answer"),
                                subject=self._subject_of(data))
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 400)
            return
        self.json_response(result)

    def _handle_bank_import(self, data: dict[str, Any]) -> None:
        try:
            import bank
            result = bank.import_questions(data.get("questions"),
                                           subject=self._subject_of(data))
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 400)
            return
        self.json_response(result)

    def _handle_exam_create(self, data: dict[str, Any]) -> None:
        import exam
        name = str(data.get("name", "")).strip()
        if not name:
            self.json_response({"error": "试卷名称不能为空"}, 400)
            return
        pid = exam.create_paper(name, str(data.get("exam_date", "")).strip(),
                                float(data.get("target", 80) or 80))
        self.json_response({"id": pid}, 201)

    def _handle_exam_add_questions(self, paper_id: int, data: dict[str, Any]) -> None:
        import exam
        if not row("SELECT 1 FROM exam_papers WHERE id = ?", (paper_id,)):
            self.json_response({"error": "试卷不存在"}, 404)
            return
        count = exam.add_questions(paper_id, data.get("questions") or [])
        self.json_response({"added": count}, 201)

    def _handle_settings_test(self, data: dict[str, Any]) -> None:
        reply = call_ai([
            {"role": "system", "content": "只回答：连接成功"},
            {"role": "user", "content": "测试连接"},
        ], max_tokens=20, route="test")
        self.json_response({"ok": True, "reply": reply})

    def _handle_fsrs_optimal(self) -> None:
        """CMRR 式最优保持率估算：基于本学科卡量与平均稳定度的解析模拟。"""
        data = rows("SELECT stability, repetition FROM problems WHERE subject = ?", (self.subject,))
        stabilities = [float(r["stability"] or 0) for r in data if int(r["repetition"] or 0) > 0]
        result = fsrs_bridge.optimal_retention(stabilities, len(data))
        result["current"] = fsrs_bridge._desired_retention()
        self.json_response(result)

    def _handle_fsrs_retention(self, data: dict[str, Any]) -> None:
        ok = fsrs_bridge.set_desired_retention(data.get("value", 0))
        self.json_response({"ok": ok})

    def _handle_keystore_unlock(self, data: dict[str, Any]) -> None:
        from ai import unlock_keyfile
        ok = unlock_keyfile(data.get("master_password", ""))
        self.json_response({"ok": ok, **display_settings()})

    def _handle_keystore_clear(self, data: dict[str, Any]) -> None:
        from ai import clear_session_key
        clear_session_key()
        self.json_response({"ok": True, **display_settings()})

    def _wants_sse(self) -> bool:
        accept = self.headers.get("Accept", "")
        return "text/event-stream" in accept

    def _sse_send(self, event: str, payload: dict[str, Any]) -> None:
        line = f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        self.wfile.write(line.encode("utf-8"))
        self.wfile.flush()

    def _serve_media(self, path: str) -> None:
        """B1：提供 /media/* 静态图片（限制在 MEDIA_DIR 内）。"""
        name = path[len("/media/"):].replace("\\", "/")
        if "/" in name or ".." in name:
            self.json_response({"error": "非法路径"}, 400)
            return
        fp = MEDIA_DIR / name
        if not fp.is_file():
            self.json_response({"error": "文件不存在"}, 404)
            return
        ext = fp.suffix.lower()
        ctype = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "application/octet-stream")
        body = fp.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self) -> None:
        if not self._csrf_ok():
            self.json_response({"error": "请求来源不被信任 (缺少 X-Requested-With)"}, 403)
            return
        path = urlparse(self.path).path
        try:
            data = self.read_json()
            if path == "/api/settings":
                self._handle_update_settings(data)
                return
            if path == "/api/profile":
                from profile import update as _profile_update
                _profile_update(data)
                self.json_response({"ok": True})
                return
            match = re.fullmatch(r"/api/problems/(\d+)", path)
            if match:
                self._handle_update_problem(int(match.group(1)), data)
                return
            match = re.fullmatch(r"/api/reviews/(\d+)/reschedule", path)
            if match:
                self._handle_reschedule_review(int(match.group(1)))
                return
            match = re.fullmatch(r"/api/graph/concepts/(\d+)", path)
            if match:
                if not graph.update_aliases(int(match.group(1)), str(data.get("aliases", ""))):
                    self.json_response({"error": "概念不存在"}, 404)
                    return
                self.json_response({"ok": True})
                return
            self.json_response({"error": "接口不存在"}, 404)
        except Exception as exc:
            self._safe_error(exc)

    def _handle_update_settings(self, data: dict[str, Any]) -> None:
        allowed = {"api_base", "model", "temperature", "fast_model", "heavy_model", "vision_model",
                   "default_subject", "hint_cache_enabled", "daily_review_cap",
                   "ai_context_tokens", "allow_local_ai"}
        values = []
        for key in allowed:
            if key in data:
                value = str(data[key]).strip()
                if key == "default_subject":
                    value = self._valid_subject(value)
                if key == "hint_cache_enabled":
                    value = "0" if value in ("0", "false", "off") else "1"
                if key == "daily_review_cap":
                    value = str(max(0, min(500, int(value) or 0)))
                if key == "temperature":
                    try:
                        value = str(max(0.0, min(2.0, float(value))))
                    except (TypeError, ValueError):
                        value = "0.3"  # 空串/非法值回退默认，防止后续 float('') 崩溃
                if key == "ai_context_tokens":
                    value = str(max(4000, min(1_000_000, int(value) or 32000)))
                if key == "allow_local_ai":
                    value = "0" if value in ("0", "false", "off") else "1"
                values.append((key, value))
        with DB_LOCK, db() as conn:
            conn.executemany("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", values)
        # 密钥不落库（R4）：仅存于内存（会话级）或 keys.enc（可选加密文件）
        key = str(data.get("api_key", "")).strip()
        if key and key != "••••••••":
            set_runtime_key(key)
        master_password = str(data.get("master_password", "")).strip()
        if master_password:
            set_master_password(master_password)
            from keystore import save_key
            if key and key != "••••••••":
                save_key(key, master_password)
        invalidate_settings_cache()
        self.json_response({"ok": True, "has_api_key": bool(display_settings().get("has_api_key"))})

    def do_DELETE(self) -> None:
        if not self._csrf_ok():
            self.json_response({"error": "请求来源不被信任 (缺少 X-Requested-With)"}, 403)
            return
        path = urlparse(self.path).path
        try:
            match = re.fullmatch(r"/api/problems/(\d+)", path)
            if match:
                with DB_LOCK, db() as conn:
                    cursor = conn.execute("DELETE FROM problems WHERE id = ?", (int(match.group(1)),))
                    if cursor.rowcount == 0:
                        self.json_response({"error": "题目不存在"}, 404)
                        return
                graph.update_progress(force=True)
                self.json_response({"ok": True})
                return
            match = re.fullmatch(r"/api/graph/concepts/(\d+)", path)
            if match:
                if not graph.delete_concept(int(match.group(1))):
                    self.json_response({"error": "概念不存在或仍有子概念/绑定题目"}, 400)
                    return
                self.json_response({"ok": True})
                return
            match = re.fullmatch(r"/api/rag/doc/(\d+)", path)
            if match:
                import rag
                if not rag.delete_doc(int(match.group(1))):
                    self.json_response({"error": "文档不存在"}, 404)
                    return
                self.json_response({"ok": True})
                return
            match = re.fullmatch(r"/api/exam/papers/(\d+)", path)
            if match:
                import exam
                if not exam.delete_paper(int(match.group(1))):
                    self.json_response({"error": "试卷不存在"}, 404)
                    return
                self.json_response({"ok": True})
                return
            match = re.fullmatch(r"/api/subjects/([A-Za-z][A-Za-z0-9_]{0,19})", path)
            if match:
                self._handle_delete_subject(match.group(1))
                return
            self.json_response({"error": "接口不存在"}, 404)
        except Exception as exc:
            self._safe_error(exc)
