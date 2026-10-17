"""ProblemsMixin — Handler 错题/提示/变式/标签/导出/媒体域。自 handler.py 原样迁移。

业务逻辑（SQL/事务/校验）已下沉到 problems.py，这里只做 HTTP 适配：
解析 query/body → 调用领域函数 → 写响应。
"""
from __future__ import annotations

import base64
import csv
import io
import json
import time
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlparse, parse_qs
from pathlib import Path

from config import LOG
import auth
from db import DB_LOCK, db, now, row, rows
from ai import (call_ai, call_ai_stream, fallback_hint, problem_prompt, extract_tags,
                generate_variants, invalidate_settings_cache, get_cached_settings)
from handler_base import (_IDEMPOTENCY, _IDEMPOTENCY_TTL, _IDEMPOTENCY_LOCK,
                          _prune_idempotency)
import interop
import problems
from resp import api_err
from ratelimit import register_failure, clear as rl_clear

# 拍照/截图录题（B1）魔数（模块级，mixin 内 _image_ext 引用）
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"

# MEDIA_DIR 动态引用：测试会重绑定 config.MEDIA_DIR，值拷贝会失同步
import config as _config


def _media_dir():
    return _config.MEDIA_DIR


class ProblemsMixin:
    def _export_token_ok(self) -> bool:
        """§1.1/§16.6 + R1/R5：导出/还原必须携带一次性 HMAC 挑战令牌。

        令牌由同源 POST /api/export/challenge 签发，60s 内单次有效、用后即焚（防重放），
        签名绑定客户端 IP（R5：换 IP 重放即拒）。EXPORT_TOKEN 仅作 HMAC 签名密钥，
        永不外发；跨源恶意网页读不到、也伪造不出。
        令牌经 query `?token=` 或头 `X-Export-Token` 传递。
        """
        qs = parse_qs(urlparse(self.path).query)
        provided = qs.get("token", [""])[0] or self.headers.get("X-Export-Token", "")
        return bool(provided) and auth.verify_export_challenge(provided, self._client_ip())

    def _client_ip(self) -> str:
        """取 TCP 对端 IP，用于失败限流归因；无法识别时按回环放宽（避免误伤）。"""
        addr = getattr(self, "client_address", None)
        if isinstance(addr, (tuple, list)) and addr:
            ip = str(addr[0])
            if ip in ("127.0.0.1", "::1", "localhost") or ip.startswith("127."):
                return "127.0.0.1"
            return ip
        return "127.0.0.1"

    def _guard_export_token(self) -> bool:
        """导出/还原令牌统一守卫（P4b 限流接入点）。

        - 有效令牌：清零该客户端失败计数，返回 True（放行）。
        - 无效令牌：登记一次失败；未超阈值返 401(FORBIDDEN)，超限返 429(RATE_LIMITED)。
        调用方：`if not self._guard_export_token(): return`。
        """
        ip = self._client_ip()
        if self._export_token_ok():
            rl_clear(ip)
            return True
        if register_failure(ip):
            api_err(self, "RATE_LIMITED")
        else:
            api_err(self, "FORBIDDEN")
        return False

    def _handle_list_problems(self) -> None:
        """支持分页与搜索: ?page=1&limit=50&q=关键词&sort=time|mastery (limit 上限 200)"""
        qs = parse_qs(urlparse(self.path).query)
        try:
            limit = min(max(int(qs.get("limit", ["50"])[0]), 1), 200)
            page = max(int(qs.get("page", ["1"])[0]), 1)
        except (ValueError, IndexError):
            limit, page = 50, 1
        self.json_response(problems.list_problems(
            self.subject,
            q=(qs.get("q", [""])[0] or "").strip(),
            sort=qs.get("sort", ["time"])[0],
            page=page,
            limit=limit,
            prereq=qs.get("prereq", [""])[0],
        ))

    def _handle_get_problem(self, problem_id: int) -> None:
        try:
            item = problems.get_problem(problem_id)
        except problems.ProblemsError as exc:
            self.json_response({"error": exc.message}, exc.status)
            return
        self.json_response(item)

    def _handle_problem_history(self, problem_id: int) -> None:
        """一道题的全部已完成复习记录（SM-2 轨迹）。"""
        self.json_response(problems.history(problem_id))

    def _handle_related_problems(self, problem_id: int) -> None:
        """同知识点 / 同课程的其他题目（排除自身，最多 3 题）。"""
        try:
            related = problems.related(problem_id, self.subject)
        except problems.ProblemsError as exc:
            self.json_response({"error": exc.message}, exc.status)
            return
        self.json_response(related)

    def _handle_duplicates(self) -> None:
        """C7 查重：按 topic 加权 + 内容 bigram 相似度，返回 top 相似题。"""
        qs = parse_qs(urlparse(self.path).query)
        content = (qs.get("content", [""])[0] or "").strip()
        topic = (qs.get("topic", [""])[0] or "").strip()
        exclude = qs.get("exclude", [""])[0]
        self.json_response({"duplicates": problems.duplicates(content, topic, exclude)})

    def _handle_create_problem(self, data: dict[str, Any]) -> None:
        rid = self.headers.get("X-Request-Id")
        if rid:
            with _IDEMPOTENCY_LOCK:  # R4：读-判-删原子化，防并发线程交错
                hit = _IDEMPOTENCY.get(rid)
                if hit is not None:
                    ts, cached = hit
                    if ts >= datetime.now().timestamp() - _IDEMPOTENCY_TTL:
                        self.json_response(cached, 201)
                        return
                    _IDEMPOTENCY.pop(rid, None)

        try:
            problem_id = problems.create(self.subject, data)
        except problems.ProblemsError as exc:
            self.json_response({"error": exc.message}, exc.status)
            return
        result = {"id": problem_id}
        if rid:
            with _IDEMPOTENCY_LOCK:  # R4：写幂等缓存与剪枝原子化
                _IDEMPOTENCY[rid] = (datetime.now().timestamp(), result)
                _prune_idempotency()
        self.json_response(result, 201)

    def _handle_update_problem(self, problem_id: int, data: dict[str, Any]) -> None:
        try:
            problems.update(problem_id, data)
        except problems.ProblemsError as exc:
            self.json_response({"error": exc.message}, exc.status)
            return
        self.json_response({"ok": True})

    def _handle_batch(self, data: dict[str, Any]) -> None:
        """批量操作：删除/标记掌握度/切换星标。"""
        action = str(data.get("action", "")).strip()
        try:
            affected = problems.batch(data.get("ids"), action)
        except problems.ProblemsError as exc:
            self.json_response({"error": exc.message}, exc.status)
            return
        if action == "delete":
            auth.audit("batch_delete", ip=self._client_ip(), detail=f"n={affected}")  # R5 审计
        self.json_response({"ok": True, "affected": affected})

    def _handle_import(self, data: dict[str, Any]) -> None:
        """导入：先自动备份当前数据库，再参数化写入，避免注入与数据丢失。"""
        try:
            result = problems.import_data(data)
        except problems.ProblemsError as exc:
            self.json_response({"error": exc.message}, exc.status)
            return
        invalidate_settings_cache()
        auth.audit("import", ip=self._client_ip(), detail=f"n={result['imported']}")  # R5 审计
        self.json_response({"ok": True, "imported": result["imported"], "backup": result["backup"]})

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
        # R3 配额守卫：必须在命中缓存之后、SSE 分支之前拦截——
        # 缓存命中不消耗 AI 额度（不计费），SSE 与非 SSE 两条通路都要付费，故在分叉前统一拦。
        if not self._ai_quota("fast"):
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
        # HTTP/1.1 keep-alive 下 SSE 无 Content-Length，必须显式关闭连接定界
        self.send_header("Connection", "close")
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
        if not self._ai_quota("heavy"):
            return  # R3：变式生成重接口
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

    def _rag_context(self, problem: dict[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
        """B3：检索个人资料（教材/笔记）相关片段，注入 AI 上下文；返回 (注入消息, 溯源列表)。

        检索 k=5 后按相对分阈值过滤（剔除沾边低质片段）；X1 增补：
        ① filter_noise 低质命中兜底（单字分词下共享常用字的假 Source）；
        ② 零命中且资料库非空时注入「资料未覆盖」硬边界，防 AI 谎称根据教材。
        """
        try:
            import rag
            query = (f"{problem.get('topic', '')} {problem.get('title', '')} "
                     f"{str(problem.get('content', ''))[:200]}")
            hits = rag.search(query, k=5)
            hits = rag.filter_relevant(hits)
            hits = rag.filter_noise(hits, query)  # X1：低质命中判噪声（宁可漏注入不假注入）
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
            frags.append(f"[Source {len(sources)}｜{src['name']}"
                         + (f" 第{page}页" if page else "") + f"] {hit['content']}")
        if not frags:
            # X1 越界抑制：资料库非空但零命中 → 注入硬边界消息——资料未覆盖须显式声明，
            # 教材外补充必须标注，防 AI 谎称「根据教材」。空库保持原行为（RAG 未启用，不制造噪音）。
            if docs:
                return [{"role": "system", "content": (
                    "用户个人资料库中未检索到与本题相关的内容（资料未覆盖本题）。\n"
                    "硬性要求：① 不得声称「根据教材/资料/笔记」；"
                    "② 如需补充资料之外的知识，必须以「🟡 AI 补充（非资料内容）」开头标注；"
                    "③ 其余内容仅基于题目本身作答。"
                )}], []
            return [], []
        return [{"role": "system", "content": (
            "以下是用户个人资料（教材/课件/笔记）中检索到的相关片段：\n" + "\n".join(frags)
            + "\n要求：解答时优先采用与上述片段一致的表述与符号；"
              "片段与题目无关时直接忽略，不得把片段中没有的内容说成出自教材；"
              "引用片段结论时可标注对应 [Source N]。"
        )}], sources

    def _handle_concept_explanation(self, data: dict[str, Any]) -> None:
        """生成概念详解（AI，草稿不落库）；前端编辑后通过 PUT /api/graph/concepts/<id> 保存。"""
        if not self._ai_quota("heavy"):
            return  # R3：生成属 heavy 档，护 API 额度
        name = str(data.get("name", "")).strip()
        if not name:
            self.json_response({"error": "概念名为空"}, 400)
            return
        # U2 引用面板注入：用户确认的教材/子图引用段落（截断防滥用后透传 AI）
        context_ref = str(data.get("context_ref", ""))[:6000]
        try:
            from ai import explain_concept
            text = explain_concept(
                name,
                str(data.get("subject", "physics")),
                str(data.get("aliases", "")),
                str(data.get("prereq", "")),
                str(data.get("succ", "")),
                str(data.get("contrast", "")),
                context_ref,
            )
            self.json_response({"explanation": text})
        except RuntimeError as exc:
            LOG.warning("概念详解生成失败: %s", exc)
            self.json_response({"error": str(exc)}, 502)

    def _handle_context_estimate(self, data: dict[str, Any]) -> None:
        """U2 引用面板：上下文 token 估算（零依赖启发式，只读、不调 AI、不计额度）。

        返回 {tokens, window, ratio}：ratio = tokens / ai_context_tokens，
        供前端「引用面板」实时展示预估 token 与占上下文窗口比例，注入前让用户看清成本。
        """
        try:
            from ai import estimate_tokens
        except Exception:
            self.json_response({"error": "estimate_tokens 不可用"}, 501)
            return
        text = str(data.get("text", ""))
        tokens = estimate_tokens(text)
        window = int(str(get_cached_settings().get("ai_context_tokens", "32000")) or 32000)
        ratio = round(tokens / window, 4) if window else 0
        self.json_response({"tokens": tokens, "window": window, "ratio": ratio})

    def _handle_extract_tags(self, data: dict[str, Any]) -> None:
        """B5：AI 自动打标签（草稿，R3 不落库）。返回建议 + 置信度，前端确认后写入。"""
        if not self._ai_quota("fast"):
            return  # R3：打标签属 fast 档（轻接口，阈值放宽）
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
        if not self._ai_quota("heavy"):
            return  # R3：视觉识别重接口，护 API 额度
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
        if not self._guard_export_token():
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
        if not self._guard_export_token():
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
        """一键还原：接收备份 JSON，工作区内重建库。

        鉴权：与导出端点同级——除 do_POST 入口的 CSRF 外，必须携带有效导出令牌。
        整库重建是最高风险操作，鉴权强度不应低于只读导出。
        """
        if not self._guard_export_token():
            return
        raw = data.get("backup") if isinstance(data, dict) else None
        if not isinstance(raw, str) or not raw.strip():
            self.json_response({"error": "缺少 backup 字段"}, 400)
            return
        try:
            import backup as backup_mod
            result = backup_mod.restore_backup(raw)
            auth.audit("restore", ip=self._client_ip(), detail="full")  # R5 审计：整库重建
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
