"""ProblemsMixin — Handler 错题/提示/变式/标签/导出/媒体域。自 handler.py 原样迁移。"""
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
from typing import Any
from urllib.parse import urlparse, parse_qs
from pathlib import Path

from config import LOG, MEDIA_DIR, EXPORT_TOKEN
from db import DB_LOCK, db, now, row, rows
import db as db_module  # 模块引用：上方 `db` 已被函数遮蔽，需用模块访问实时 DB_PATH
from ai import (call_ai, call_ai_stream, fallback_hint, problem_prompt, extract_tags,
                generate_variants, invalidate_settings_cache, get_cached_settings)
from errors import normalize_error_type
from review import clamp_mastery
from handler_base import (X_HEADER, X_VALUE, _IDEMPOTENCY, _IDEMPOTENCY_TTL,
                          _as_str_list, _interleave, _prune_idempotency)
import graph
import interop

# 拍照/截图录题（B1）魔数（模块级，mixin 内 _image_ext 引用）
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"

# MEDIA_DIR 动态引用：测试会重绑定 config.MEDIA_DIR，值拷贝会失同步
import config as _config


def _media_dir():
    return _config.MEDIA_DIR


class ProblemsMixin:
    def _export_token_ok(self) -> bool:
        """§1.1/§16.6：导出整库/错题库必须携带一次性本地令牌。

        令牌通过 query `?token=` 或头 `X-Export-Token` 传递；同源应用从
        /api/bootstrap 取得后注入下载链接，跨源网页无法读取故无法导出。
        """
        qs = parse_qs(urlparse(self.path).query)
        provided = qs.get("token", [""])[0] or self.headers.get("X-Export-Token", "")
        return bool(provided) and provided == EXPORT_TOKEN

    def _handle_list_problems(self) -> None:
        """支持分页与搜索: ?page=1&limit=50&q=关键词&sort=time|mastery (limit 上限 200)"""
        qs = parse_qs(urlparse(self.path).query)
        try:
            limit = min(max(int(qs.get("limit", ["50"])[0]), 1), 200)
            page = max(int(qs.get("page", ["1"])[0]), 1)
        except (ValueError, IndexError):
            limit, page = 50, 1
        q = (qs.get("q", [""])[0] or "").strip()
        sort = qs.get("sort", ["time"])[0]
        order = {"time": "id DESC", "mastery": "mastery ASC"}.get(sort, "id DESC")
        offset = (page - 1) * limit

        if q:
            like = f"%{q}%"
            where = " WHERE subject = ? AND (title LIKE ? OR topic LIKE ? OR course LIKE ?)"
            params: tuple[Any, ...] = (self.subject, like, like, like)
        else:
            where, params = " WHERE subject = ?", (self.subject,)

        # A2 先修模式：?prereq=<concept_id> 过滤出该概念先修链上的历史错题
        prereq_param = qs.get("prereq", [""])[0]
        if prereq_param.isdigit():
            chain = graph.prereq_chain(int(prereq_param))
            if chain:
                cond = " OR ".join("concept_ids LIKE ?" for _ in chain)
                chain_params = tuple(f"%,{cid},%" for cid in chain)
                if where:
                    where += f" AND ({cond})"
                else:
                    where = f" WHERE {cond}"
                params = params + chain_params

        items = rows(
            f"SELECT id, title, course, topic, content, my_attempt, error_path, fix_action, error_type, mastery, starred, tags, tags_status, created_at, updated_at FROM problems{where} ORDER BY {order} LIMIT ? OFFSET ?",
            params + (limit, offset),
        )
        total_row = row(f"SELECT COUNT(*) AS count FROM problems{where}", params)
        total = total_row["count"] if total_row else 0
        # 一次窗口函数查询拉回所有题的最近 3 次评分（替代每道题单独查）
        if items:
            ids = [item["id"] for item in items]
            placeholders = ",".join("?" for _ in ids)
            mini_rows = rows(f"""
                SELECT problem_id, result FROM (
                    SELECT problem_id, result, id,
                        ROW_NUMBER() OVER (PARTITION BY problem_id ORDER BY id DESC) AS rn
                    FROM reviews WHERE completed = 1 AND problem_id IN ({placeholders})
                ) WHERE rn <= 3 ORDER BY problem_id, id ASC
            """, tuple(ids))
            by_pid: dict[int, list[str]] = {pid: [] for pid in ids}
            for mr in mini_rows:
                by_pid[mr["problem_id"]].append(mr["result"])
            for item in items:
                item["recent_results"] = by_pid.get(item["id"], [])
        self.json_response({
            "items": items, "total": total, "page": page, "limit": limit,
            "pages": (total + limit - 1) // limit,
        })

    def _handle_get_problem(self, problem_id: int) -> None:
        item = row("SELECT * FROM problems WHERE id = ?", (problem_id,))
        if not item:
            self.json_response({"error": "题目不存在"}, 404)
            return
        try:
            item["tags"] = json.loads(item["tags"]) if item["tags"] else []
        except (json.JSONDecodeError, TypeError):
            item["tags"] = []
        try:
            item["variants"] = json.loads(item["variants"]) if item["variants"] else []
        except (json.JSONDecodeError, TypeError):
            item["variants"] = []
        # A2：concept_ids 解析 + 先修掌握度告警
        item["concept_ids"] = graph.concept_ids_to_list(item.get("concept_ids") or "")
        item["prereq_warnings"] = graph.prereq_warnings(problem_id)
        # A8：一题多解
        try:
            item["methods"] = json.loads(item["methods"]) if item.get("methods") else []
        except (json.JSONDecodeError, TypeError):
            item["methods"] = []
        # A5：Feynman 自评表（已保存的最新一条）
        feynman = row(
            "SELECT self_review FROM oral_sessions "
            "WHERE problem_id = ? AND mode = 'feynman' AND self_review != '' "
            "ORDER BY id DESC LIMIT 1",
            (problem_id,),
        )
        if feynman:
            try:
                item["feynman_self_review"] = json.loads(feynman["self_review"])
            except json.JSONDecodeError:
                item["feynman_self_review"] = None
        else:
            item["feynman_self_review"] = None
        # B1：图片附件列表
        item["media_list"] = [p for p in (item.get("media_path") or "").split(",") if p.strip()]
        item["hints"] = rows("SELECT level, content, created_at FROM hints WHERE problem_id = ? ORDER BY level", (problem_id,))
        self.json_response(item)

    def _handle_problem_history(self, problem_id: int) -> None:
        """一道题的全部已完成复习记录（SM-2 轨迹）。"""
        history = rows("""
            SELECT due_date, result, interval_days, created_at
            FROM reviews WHERE problem_id = ? AND completed = 1
            ORDER BY id ASC
        """, (problem_id,))
        self.json_response(history)

    def _handle_related_problems(self, problem_id: int) -> None:
        """同知识点 / 同课程的其他题目（排除自身，最多 3 题）。"""
        p = row("SELECT topic, course FROM problems WHERE id = ?", (problem_id,))
        if not p:
            self.json_response({"error": "题目不存在"}, 404)
            return
        topic = p["topic"] or ""
        course = p["course"] or ""
        related = rows(
            "SELECT id, title, course, topic, mastery FROM problems WHERE id != ? AND subject = ? AND (topic = ? OR course = ?) ORDER BY id DESC LIMIT 3",
            (problem_id, self.subject, topic, course),
        )
        self.json_response(related)

    def _handle_duplicates(self) -> None:
        """C7 查重：按 topic 加权 + 内容 bigram 相似度，返回 top 相似题。"""
        qs = parse_qs(urlparse(self.path).query)
        content = (qs.get("content", [""])[0] or "").strip()
        topic = (qs.get("topic", [""])[0] or "").strip()
        exclude = qs.get("exclude", [""])[0]
        if not content:
            self.json_response({"duplicates": []})
            return
        candidates = rows("SELECT id, title, topic, content FROM problems WHERE id <> ? ORDER BY id DESC LIMIT 300",
                          (exclude or 0,))
        scored = []
        for c in candidates:
            sim = self._similarity(content, c["content"])
            if topic and topic == c["topic"]:
                sim = min(1.0, sim + 0.15)
            if sim >= 0.35:
                scored.append({
                    "id": c["id"], "title": c["title"], "topic": c["topic"],
                    "similarity": round(sim, 2),
                })
        scored.sort(key=lambda x: -x["similarity"])
        self.json_response({"duplicates": scored[:5]})

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """C7 字符 bigram Jaccard 相似度（零依赖，中文/英文通用）。"""
        a = re.sub(r"\s+", "", a or "")
        b = re.sub(r"\s+", "", b or "")
        if not a or not b:
            return 0.0
        def bigrams(s: str) -> set[str]:
            return {s[i:i + 2] for i in range(max(0, len(s) - 1))} if len(s) > 1 else {s}
        ba, bb = bigrams(a), bigrams(b)
        if not ba and not bb:
            return 1.0
        union = ba | bb
        return len(ba & bb) / len(union) if union else 0.0

    def _handle_create_problem(self, data: dict[str, Any]) -> None:
        rid = self.headers.get("X-Request-Id")
        if rid and rid in _IDEMPOTENCY:
            ts, cached = _IDEMPOTENCY[rid]
            if ts >= datetime.now().timestamp() - _IDEMPOTENCY_TTL:
                self.json_response(cached, 201)
                return
            _IDEMPOTENCY.pop(rid, None)

        title = str(data.get("title", "")).strip()
        content = str(data.get("content", "")).strip()
        if not title or not content:
            self.json_response({"error": "标题和题目内容不能为空"}, 400)
            return
        stamp = now()
        error_type = normalize_error_type(data.get("error_type", "待诊断"))
        tags = json.dumps(data.get("tags", []), ensure_ascii=False)
        tags_status = "confirmed" if data.get("tags") else "none"
        methods = json.dumps(_as_str_list(data.get("methods")), ensure_ascii=False)
        # A2：显式 concept_ids 校验存在后落库；未提供则稍后自动绑定
        raw_concepts = data.get("concept_ids") or []
        if isinstance(raw_concepts, list):
            ids = [cid for cid in raw_concepts if isinstance(cid, int)]
            concept_csv = f",{','.join(str(cid) for cid in ids)}," if ids else ""
        else:
            concept_csv = ""
        with DB_LOCK, db() as conn:
            cursor = conn.execute("""
                INSERT INTO problems(title, course, topic, content, my_attempt, error_type,
                                     error_path, trap_note, shortcut, fix_action, tags, tags_status,
                                     concept_ids, media_path, methods, subject, mastery, ease_factor, repetition,
                                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 2.5, 0, ?, ?)
            """, (
                title, str(data.get("course", "")).strip(), str(data.get("topic", "")).strip(),
                content, str(data.get("my_attempt", "")).strip(), error_type,
                str(data.get("error_path", "")).strip(), str(data.get("trap_note", "")).strip(),
                str(data.get("shortcut", "")).strip(), str(data.get("fix_action", "")).strip(),
                tags, tags_status, concept_csv,
                self._normalize_media_paths(data.get("media_path", "")),
                methods,
                self.subject, clamp_mastery(int(data.get("mastery", 1))), stamp, stamp,
            ))
            problem_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO reviews(problem_id, due_date, interval_days, created_at) VALUES (?, ?, 1, ?)",
                (problem_id, (date.today() + timedelta(days=1)).isoformat(), stamp),
            )
        if not concept_csv:
            graph.bind_problem(problem_id)
        result = {"id": problem_id}
        if rid:
            _IDEMPOTENCY[rid] = (datetime.now().timestamp(), result)
            _prune_idempotency()
        self.json_response(result, 201)

    def _handle_update_problem(self, problem_id: int, data: dict[str, Any]) -> None:
        existing = row("SELECT * FROM problems WHERE id = ?", (problem_id,))
        if not existing:
            self.json_response({"error": "题目不存在"}, 404)
            return
        fields = ["title", "course", "topic", "content", "my_attempt", "error_type",
                  "error_path", "trap_note", "shortcut", "fix_action", "mastery", "starred"]
        merged = {field: data.get(field, existing[field]) for field in fields}
        merged["mastery"] = clamp_mastery(int(merged["mastery"]))
        merged["title"] = str(merged["title"]).strip()
        merged["content"] = str(merged["content"]).strip()
        merged["error_type"] = normalize_error_type(merged["error_type"])
        if not merged["title"] or not merged["content"]:
            self.json_response({"error": "标题和题目内容不能为空"}, 400)
            return
        # B5（R3）：tags 显式提交才算「草稿确认」，落库并置 confirmed；否则保留原状
        tags = existing["tags"]
        tags_status = existing["tags_status"]
        if data.get("tags") is not None:
            tags = json.dumps(data["tags"], ensure_ascii=False)
            tags_status = "confirmed"
        # A8：methods 显式提交则整体覆盖（结构校验：只收字符串数组）
        methods = existing["methods"]
        if data.get("methods") is not None:
            methods = json.dumps(_as_str_list(data["methods"]), ensure_ascii=False)
        with DB_LOCK, db() as conn:
            conn.execute("""
                UPDATE problems SET title=?, course=?, topic=?, content=?, my_attempt=?, error_type=?,
                                    error_path=?, trap_note=?, shortcut=?, fix_action=?,
                                    mastery=?, starred=?, tags=?, tags_status=?, methods=?, updated_at=?
                WHERE id=?
            """, tuple(merged[field] for field in fields) + (tags, tags_status, methods, now(), problem_id))
        self.json_response({"ok": True})

    def _handle_batch(self, data: dict[str, Any]) -> None:
        """批量操作：删除/标记掌握度/切换星标。"""
        ids = data.get("ids")
        action = str(data.get("action", "")).strip()
        if not isinstance(ids, list) or not ids or not action:
            self.json_response({"error": "参数不合法 (ids/action)"}, 400)
            return
        with DB_LOCK, db() as conn:
            for pid in ids:
                pid = int(pid)
                if action == "delete":
                    conn.execute("DELETE FROM problems WHERE id = ?", (pid,))
                elif action == "star":
                    conn.execute("UPDATE problems SET starred = CASE WHEN starred THEN 0 ELSE 1 END, updated_at = ? WHERE id = ?",
                                 (now(), pid))
        self.json_response({"ok": True, "affected": len(ids)})

    def _handle_import(self, data: dict[str, Any]) -> None:
        """导入：先自动备份当前数据库，再参数化写入，避免注入与数据丢失。"""
        problems = data.get("problems")
        if not isinstance(problems, list):
            self.json_response({"error": "导入数据格式错误（缺少 problems 列表）"}, 400)
            return
        # 版本兼容性校验（防未来 schema 不一致的备份被误加载）
        data_version = int(data.get("version", 1))
        if data_version > 1:
            self.json_response({"error": f"备份来自更新的版本 (v{data_version})，请升级应用后再导入"}, 400)
            return
        # 自动备份到 backups/（与每日自动备份同目录，避免根目录堆积 .bak 文件）
        from config import APP_DIR
        backup_dir = APP_DIR / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"import_{now().replace(':', '').replace('-', '')}.db"
        try:
            shutil.copy(db_module.DB_PATH, backup)
        except OSError as exc:
            self.json_response({"error": f"备份失败: {exc}"}, 500)
            return
        # 只保留最近 7 份导入备份（防长期累积）
        try:
            olds = sorted(backup_dir.glob("import_*.db"))
            for old in olds[:-7]:
                old.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            with DB_LOCK, db() as conn:
                conn.execute("DELETE FROM hints")
                conn.execute("DELETE FROM reviews")
                conn.execute("DELETE FROM problems")
                for p in problems:
                    if not isinstance(p, dict):
                        continue
                    pid = int(p.get("id", 0))
                    title = str(p.get("title", "")).strip()
                    content = str(p.get("content", "")).strip()
                    if not title or not content:
                        continue
                    conn.execute("""
                        INSERT INTO problems(id, title, course, topic, content, my_attempt, error_type,
                                             error_path, trap_note, shortcut, fix_action,
                                             tags, tags_status,
                                             mastery, ease_factor, repetition, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 2.5, 0, ?, ?)
                    """, (
                        pid, title, str(p.get("course", "")).strip(), str(p.get("topic", "")).strip(),
                        content, str(p.get("my_attempt", "")).strip(), normalize_error_type(p.get("error_type", "待诊断")),
                        str(p.get("error_path", "")).strip(), str(p.get("trap_note", "")).strip(),
                        str(p.get("shortcut", "")).strip(), str(p.get("fix_action", "")).strip(),
                        json.dumps(p.get("tags", []), ensure_ascii=False), str(p.get("tags_status", "none")).strip(),
                        clamp_mastery(int(p.get("mastery", 1))),
                        str(p.get("created_at", now())), str(p.get("updated_at", now())),
                    ))
                # 导入提示记录
                for h in data.get("hints", []):
                    if not isinstance(h, dict):
                        continue
                    conn.execute(
                        "INSERT INTO hints(problem_id, level, content, created_at) VALUES (?, ?, ?, ?)",
                        (int(h.get("problem_id", 0)), int(h.get("level", 1)),
                         str(h.get("content", "")).strip(), str(h.get("created_at", now()))),
                    )
                # 导入复习记录
                for rv in data.get("reviews", []):
                    if not isinstance(rv, dict):
                        continue
                    conn.execute(
                        "INSERT INTO reviews(problem_id, due_date, interval_days, result, completed, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (int(rv.get("problem_id", 0)), str(rv.get("due_date", "")).strip(),
                         int(rv.get("interval_days", 1)), str(rv.get("result", "")).strip(),
                         int(rv.get("completed", 0)), str(rv.get("created_at", now()))),
                    )
        except (ValueError, KeyError) as exc:
            self.json_response({"error": f"导入失败: {exc}"}, 400)
            return
        invalidate_settings_cache()
        self.json_response({"ok": True, "imported": len(problems), "backup": str(backup)})

    def _handle_hint(self, problem_id: int, data: dict[str, Any]) -> None:
        level = max(1, min(4, int(data.get("level", 1))))
        lang = "en" if str(data.get("lang", "zh")).lower().startswith("en") else "zh"
        problem = row("SELECT * FROM problems WHERE id = ?", (problem_id,))
        if not problem:
            self.json_response({"error": "题目不存在"}, 404)
            return
        # A6 诊断门：最近一次复习失败（忘记/模糊）时，一级提示附加诊断建议
        diagnose = False
        last_result = row("""
            SELECT result FROM reviews WHERE problem_id = ? AND completed = 1
            ORDER BY id DESC LIMIT 1
        """, (problem_id,))
        if last_result and last_result["result"].isdigit() and int(last_result["result"]) <= 2:
            diagnose = True
        existing = row("SELECT content FROM hints WHERE problem_id = ? AND level = ?", (problem_id, level))
        if existing:
            self.json_response({"content": existing["content"], "source": "saved", "cached": True,
                                "diagnose": diagnose})
            return
        # 提示缓存用户可控（R3）：关闭后 AI 生成的提示不落库
        cache_enabled = get_cached_settings().get("hint_cache_enabled", "1") != "0"
        rag_messages, rag_sources = self._rag_context(problem)
        if self._wants_sse():
            self._stream_hint(problem, level, diagnose, rag_messages, rag_sources, lang,
                              cache_enabled=cache_enabled)
            return
        source = "ai"
        try:
            hint = call_ai(problem_prompt(problem, level, lang) + rag_messages, tier="fast", route="hint")
        except Exception as exc:
            hint = fallback_hint(problem, level, lang)
            source = "fallback"
            LOG.warning("提示降级 (problem=%s, level=%d): %s", problem_id, level, exc)
        cached = False
        if cache_enabled:
            with DB_LOCK, db() as conn:
                conn.execute(
                    "INSERT INTO hints(problem_id, level, content, created_at) VALUES (?, ?, ?, ?)",
                    (problem_id, level, hint, now()),
                )
            cached = True
        self.json_response({"content": hint, "source": source, "cached": cached,
                            "diagnose": diagnose, "sources": rag_sources})

    def _stream_hint(self, problem: dict[str, Any], level: int, diagnose: bool = False,
                     rag_messages: list[dict[str, str]] | None = None,
                     rag_sources: list[dict[str, Any]] | None = None,
                     lang: str = "zh", cache_enabled: bool = True) -> None:
        """SSE 流式提示：start → delta* → done | error（含 partial）。"""
        rag_messages = rag_messages or []
        rag_sources = rag_sources or []
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self._sse_send("start", {"problem_id": problem["id"], "level": level})
        if rag_sources:
            self._sse_send("sources", {"sources": rag_sources})
        collected: list[str] = []
        try:
            chunks = call_ai_stream(problem_prompt(problem, level, lang) + rag_messages, tier="fast", route="hint")
            for delta in chunks:
                collected.append(delta)
                self._sse_send("delta", {"delta": delta})
            hint = "".join(collected).strip()
            if not hint:
                raise RuntimeError("AI 流式返回为空")
            if cache_enabled:
                with DB_LOCK, db() as conn:
                    conn.execute(
                        "INSERT INTO hints(problem_id, level, content, created_at) VALUES (?, ?, ?, ?)",
                        (problem["id"], level, hint, now()),
                    )
            self._sse_send("done", {"content": hint, "source": "ai", "cached": cache_enabled,
                                    "diagnose": diagnose, "sources": rag_sources})
        except Exception as exc:
            LOG.warning("流式提示降级 (problem=%s, level=%d): %s", problem["id"], level, exc)
            fallback = fallback_hint(problem, level, lang)
            if cache_enabled:
                try:
                    with DB_LOCK, db() as conn:
                        conn.execute(
                            "INSERT INTO hints(problem_id, level, content, created_at) VALUES (?, ?, ?, ?)",
                            (problem["id"], level, fallback, now()),
                        )
                except Exception:
                    pass
            self._sse_send("error", {
                "partial": "".join(collected),
                "fallback": fallback,
            })

    def _handle_generate_variants(self, problem_id: int) -> None:
        """A4：生成 3 道变式（AI 或离线模板），仅返回草稿不落库（R3）。"""
        problem = row("SELECT * FROM problems WHERE id = ?", (problem_id,))
        if not problem:
            self.json_response({"error": "题目不存在"}, 404)
            return
        variants_source, variants = generate_variants(problem)
        self.json_response({"variants": variants, "source": variants_source})

    def _handle_save_variants(self, problem_id: int, data: dict[str, Any]) -> None:
        """A4：用户确认后保存变式到题根（R3 确认落库）。"""
        variants = data.get("variants")
        if not isinstance(variants, list) or not variants:
            self.json_response({"error": "变式列表不能为空"}, 400)
            return
        clean = []
        for v in variants:
            if not isinstance(v, dict):
                continue
            mode = str(v.get("mode", "")).strip()
            title = str(v.get("title", "")).strip()
            content = str(v.get("content", "")).strip()
            answer = str(v.get("answer", "")).strip()
            if title and content:
                clean.append({"mode": mode, "title": title, "content": content, "answer": answer})
        if not clean:
            self.json_response({"error": "变式内容不合法"}, 400)
            return
        existing = row("SELECT variants FROM problems WHERE id = ?", (problem_id,))
        if not existing:
            self.json_response({"error": "题目不存在"}, 404)
            return
        try:
            old = json.loads(existing["variants"]) if existing["variants"] else []
        except json.JSONDecodeError:
            old = []
        merged = old + clean
        with DB_LOCK, db() as conn:
            conn.execute("UPDATE problems SET variants = ?, updated_at = ? WHERE id = ?",
                         (json.dumps(merged, ensure_ascii=False), now(), problem_id))
        self.json_response({"ok": True, "count": len(clean), "total": len(merged)})

    @staticmethod
    def _log_variant_result(conn: Any, review: dict[str, Any], rating: int) -> None:
        """A4：变式复习后回写题根质量分（correct/total），低正确率变式自动降权。"""
        vid = int(review.get("variant_id") or 0)
        if not vid:
            return
        p = conn.execute("SELECT variants FROM problems WHERE id = ?", (review["problem_id"],)).fetchone()
        if not p or not p["variants"]:
            return
        try:
            variants = json.loads(p["variants"])
        except json.JSONDecodeError:
            return
        idx = vid - 1
        if not (0 <= idx < len(variants)):
            return
        v = variants[idx]
        v["correct"] = int(v.get("correct", 0)) + (1 if rating >= 3 else 0)
        v["total"] = int(v.get("total", 0)) + 1
        conn.execute("UPDATE problems SET variants = ? WHERE id = ?",
                     (json.dumps(variants, ensure_ascii=False), review["problem_id"]))

    @staticmethod
    def _log_mastery(conn: Any, subject: str = "physics") -> None:
        """记录当日学科掌握度均值，每天每学科保留一条（供趋势图）。"""
        r = conn.execute("SELECT AVG(mastery) AS a, COUNT(*) AS c FROM problems WHERE subject = ?", (subject,)).fetchone()
        avg = round(r["a"] or 0, 2)
        today = date.today().isoformat()
        conn.execute("DELETE FROM mastery_log WHERE day = ? AND subject = ?", (today, subject))
        conn.execute(
            "INSERT INTO mastery_log(day, avg_mastery, count, subject) VALUES (?, ?, ?, ?)",
            (today, avg, r["c"] or 0, subject),
        )

    def _rag_context(self, problem: dict[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
        """B3：检索个人资料（教材/笔记）相关片段，注入 AI 上下文；返回 (注入消息, 溯源列表)。"""
        try:
            import rag
            hits = rag.search(f"{problem.get('topic', '')} {problem.get('title', '')} "
                              f"{str(problem.get('content', ''))[:200]}", k=2)
        except Exception:
            return [], []
        docs = {d["id"]: d for d in rag.list_docs()}
        sources: list[dict[str, Any]] = []
        frags: list[str] = []
        for hit in hits:
            doc = docs.get(hit["doc_id"])
            if not doc:
                continue
            page = hit.get("page") or 0
            src = {"path": doc["source_path"], "page": page,
                   "name": Path(doc["source_path"]).name}
            sources.append(src)
            frags.append(f"[{src['name']}" + (f" 第{page}页" if page else "") + f"] {hit['content']}")
        if not frags:
            return [], []
        return [{"role": "system", "content": (
            "以下是用户个人资料（教材/课件/笔记）中检索到的相关片段，"
            "解答时应优先基于这些片段给出与教材一致的表述：\n" + "\n".join(frags)
        )}], sources

    def _handle_extract_tags(self, data: dict[str, Any]) -> None:
        """B5：AI 自动打标签（草稿，R3 不落库）。返回建议 + 置信度，前端确认后写入。"""
        title = str(data.get("title", "")).strip()
        content = str(data.get("content", "")).strip()
        if not title or not content:
            self.json_response({"error": "标题和题目内容不能为空"}, 400)
            return
        result = extract_tags(
            title,
            content,
            str(data.get("course", "")).strip(),
            str(data.get("topic", "")).strip(),
        )
        self.json_response(result)

    def _handle_upload_photo(self, data: dict[str, Any]) -> None:
        """B1：上传截图/照片到 media/（魔数校验 + 大小限制），返回相对路径。"""
        raw = str(data.get("data", "")).strip()
        if not raw:
            raise ValueError("缺少图片数据")
        try:
            blob = base64.b64decode(raw, validate=True)
        except Exception:
            raise ValueError("图片数据不是合法的 base64")
        if not blob:
            raise ValueError("图片为空")
        if len(blob) > 8 * 1024 * 1024:
            raise ValueError("图片过大（上限 8MB）")
        ext = self._image_ext(blob)
        if not ext:
            raise ValueError("仅支持 PNG/JPEG 图片")
        _media_dir().mkdir(parents=True, exist_ok=True)
        fname = f"{int(time.time())}_{uuid.uuid4().hex[:8]}.{ext}"
        (_media_dir() / fname).write_bytes(blob)
        rel = f"media/{fname}"
        self.json_response({"path": rel, "url": f"/{rel}"})

    def _handle_extract_photo(self, data: dict[str, Any]) -> None:
        """B1：视觉模型识别题目 → 卡片草稿（R3 不落库）；无 vision 降级为纯附件。"""
        fp = self._media_file(str(data.get("media_path", "")).strip())
        if not fp:
            raise ValueError("图片不存在")
        blob = fp.read_bytes()
        mime = "image/png" if self._image_ext(blob) == "png" else "image/jpeg"
        uri = f"data:{mime};base64," + base64.b64encode(blob).decode("ascii")
        try:
            from ai import call_ai_vision
            raw = call_ai_vision(
                "请识别图片中的物理题目并输出 JSON（只输出 JSON，不要其它文字）："
                '{"title": "题目概要（一句话）", "content": "完整题干与选项", '
                '"options": ["A. ...", "B. ..."], "answer": "正确答案", '
                '"analysis": "解析要点", "topic": "所属知识点"}',
                uri,
            )
            draft = json.loads(raw)
            if not isinstance(draft, dict):
                raise ValueError("识别结果格式错误")
            self.json_response({"draft": {
                "title": str(draft.get("title", "")).strip(),
                "content": str(draft.get("content", "")).strip(),
                "options": draft.get("options", []),
                "answer": str(draft.get("answer", "")).strip(),
                "analysis": str(draft.get("analysis", "")).strip(),
                "topic": str(draft.get("topic", "")).strip(),
            }})
        except (ValueError, RuntimeError) as exc:
            # 未配置 vision 模型或网络不可达：图片仅作附件 + 手动录入（方案降级路径）
            LOG.warning("视觉识别不可用，降级为附件模式: %s", exc)
            self.json_response({"draft": None, "degraded": True, "error": str(exc)})

    @staticmethod
    def _image_ext(blob: bytes) -> str:
        if blob.startswith(_PNG_MAGIC):
            return "png"
        if blob.startswith(_JPEG_MAGIC):
            return "jpg"
        return ""

    @staticmethod
    def _normalize_media_paths(value: Any) -> str:
        """拼接图片相对路径（逗号分隔，去空去重，仅允许 media/ 前缀）。"""
        if isinstance(value, list):
            parts = [str(p).strip() for p in value]
        else:
            parts = str(value or "").split(",")
        seen: list[str] = []
        for p in parts:
            p = p.strip().replace("\\", "/")
            if p.startswith("media/") and ".." not in p and p not in seen:
                seen.append(p)
        return ",".join(seen)

    @staticmethod
    def _media_file(rel: str) -> Path | None:
        """校验 media/ 相对路径并返回工作区内文件路径（防目录穿越）。"""
        rel = str(rel or "").strip().replace("\\", "/")
        if not rel.startswith("media/"):
            return None
        fp = (_media_dir().parent / rel).resolve()
        if _media_dir().resolve() not in fp.parents and fp.parent != _media_dir().resolve():
            return None
        return fp if fp.is_file() else None

    def _handle_export(self) -> None:
        """只读导出。?format=json|anki-csv|ics|csv|md（默认 json）。需导出令牌（§16.6）。"""
        if not self._export_token_ok():
            self.json_response({"error": "缺少有效的导出令牌（?token= 或 X-Export-Token）"}, 401)
            return
        qs = parse_qs(urlparse(self.path).query)
        fmt = (qs.get("format", ["json"])[0] or "json").strip()
        if fmt == "anki-csv":
            self._export_anki_csv()
            return
        if fmt == "ics":
            self._export_ics()
            return
        if fmt in ("csv", "md"):
            include_answers = (qs.get("answers", ["1"])[0] or "1") not in ("0", "false", "off")
            if fmt == "csv":
                body = interop.export_csv(self.subject, include_answers)
                self._text_response(body, "text/csv; charset=utf-8",
                                    f"learnos-{self.subject}.csv")
            else:
                body = interop.export_md(self.subject, include_answers)
                self._text_response(body, "text/markdown; charset=utf-8",
                                    f"learnos-{self.subject}.md")
            return
        problems = rows("SELECT id, title, course, topic, content, my_attempt, error_type, error_path, trap_note, shortcut, fix_action, tags, tags_status, mastery, created_at, updated_at, subject FROM problems WHERE subject = ? ORDER BY id", (self.subject,))
        for p in problems:
            try:
                p["tags"] = json.loads(p["tags"]) if p["tags"] else []
            except (json.JSONDecodeError, TypeError):
                p["tags"] = []
        data = {
            "version": 1,
            "exported_at": now(),
            "problems": problems,
            "hints": rows("SELECT problem_id, level, content, created_at FROM hints ORDER BY id"),
            "reviews": rows("SELECT problem_id, due_date, interval_days, result, completed, created_at FROM reviews ORDER BY id"),
        }
        self.json_response(data)

    def _handle_backup_export(self) -> None:
        """一键备份：全库 JSON 下载。需导出令牌（§16.6）。"""
        if not self._export_token_ok():
            self.json_response({"error": "缺少有效的导出令牌（?token= 或 X-Export-Token）"}, 401)
            return
        import backup as backup_mod
        data = backup_mod.export_backup()
        body = json.dumps(data, ensure_ascii=False, indent=1)
        self._text_response(
            body,
            "application/json",
            f"learnos-backup-{time.strftime('%Y%m%d-%H%M%S')}.json",
        )

    def _handle_backup_restore(self, data) -> None:
        """一键还原：接收备份 JSON，工作区内重建库。"""
        raw = data.get("backup") if isinstance(data, dict) else None
        if not isinstance(raw, str) or not raw.strip():
            self.json_response({"error": "缺少 backup 字段"}, 400)
            return
        try:
            import backup as backup_mod
            result = backup_mod.restore_backup(raw)
            self.json_response({"ok": True, **result})
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 400)

    def _text_response(self, body: str, content_type: str, filename: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _export_anki_csv(self) -> None:
        """Anki 导入 CSV：question|answer|tags（UTF-8 BOM，兼容 Anki 桌面端）。"""
        problems = rows("""
            SELECT p.id, p.title, p.course, p.topic, p.content, p.my_attempt, p.tags,
                   p.error_path, p.trap_note, p.shortcut, p.fix_action,
                   (SELECT GROUP_CONCAT(content, '\n') FROM hints h WHERE h.problem_id = p.id AND h.level = 3) AS answer_hint
            FROM problems p WHERE p.subject = ? ORDER BY p.id
        """, (self.subject,))
        buf = io.StringIO()
        writer = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\n")
        for p in problems:
            front = f"{p['title']}\n{p['content']}".strip()
            back_parts = []
            if p.get("my_attempt"):
                back_parts.append(f"我的尝试：{p['my_attempt']}")
            if p.get("answer_hint"):
                back_parts.append(f"解题框架：{p['answer_hint']}")
            if p.get("shortcut"):
                back_parts.append(f"捷径：{p['shortcut']}")
            if p.get("fix_action"):
                back_parts.append(f"改进：{p['fix_action']}")
            back = "\n".join(back_parts) or "（无解析）"
            tags = " ".join(t for t in [p["course"], p["topic"]] if t)
            try:
                tag_list = json.loads(p.get("tags") or "[]")
                if isinstance(tag_list, list):
                    tags = " ".join(str(t).replace(":", "_") for t in tag_list if str(t).strip())
            except json.JSONDecodeError:
                pass
            writer.writerow([front, back, tags])
        self._text_response("\ufeff" + buf.getvalue(), "text/csv; charset=utf-8", "learnos_anki.csv")

    def _export_ics(self) -> None:
        """复习日程 .ics：未完成的 due 复习任务导出为 VEVENT。"""
        due = rows("SELECT r.id, r.due_date, r.interval_days, p.title FROM reviews r JOIN problems p ON p.id = r.problem_id WHERE r.completed = 0 AND p.subject = ? ORDER BY r.due_date", (self.subject,))
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//LearnOS//CN",
            "CALSCALE:GREGORIAN",
            "X-WR-CALNAME:物理复习日程",
        ]
        for r in due:
            uid = f"pso-review-{r['id']}@learnos-os"
            stamp = r["due_date"].replace("-", "") + "T090000"
            summary = f"复习：{r['title']}"
            lines += [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTART:{stamp}",
                f"DTEND:{stamp}",
                f"SUMMARY:{summary}",
                "END:VEVENT",
            ]
        lines.append("END:VCALENDAR")
        self._text_response("\r\n".join(lines), "text/calendar; charset=utf-8", "learnos_review.ics")
