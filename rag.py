"""B3 个人资料 RAG：教材/课件/笔记摄取、BM25 检索（标准库）、溯源。

- 摄取：工作区内 md/txt 直读（UTF-8，GBK 兜底）；PDF 需 vendor 内 pdfminer（可选）。
- 分块：段落聚合约 500 字，重叠 60 字。
- 检索：BM25 为主（零依赖）；SQLite FTS5 可用时并行检索合并结果（可选增强）。
- 溯源：结果携带 source_path + page，点击经 /api/rag/open 打开本地文件。
"""
from __future__ import annotations

import json
import math
import re
import struct
import threading
from pathlib import Path
from typing import Any

import config
from db import DB_LOCK, db, now, row, rows
from config import APP_DIR, LOG

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")
_STOP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are",
         "with", "from", "by", "at", "be", "it", "this", "that"}


def _tokenize(text: str) -> list[str]:
    """英文按词 + 中文按单字。"""
    toks = [t.lower() for t in _TOKEN_RE.findall(text)]
    return [t for t in toks if t not in _STOP]


def _freq(tokens: list[str]) -> dict[str, int]:
    freq: dict[str, int] = {}
    for t in tokens:
        freq[t] = freq.get(t, 0) + 1
    return freq


def _safe_relative(path: str) -> Path | None:
    """校验路径在工作区内（APP_DIR 内），返回解析后的 Path。"""
    raw = str(path or "").strip()
    if not raw:
        return None
    fp = (APP_DIR / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    root = APP_DIR.resolve()
    try:
        fp.relative_to(root)
    except ValueError:
        LOG.warning("RAG 摄取路径越界被拒: %s", raw)
        return None
    return fp


def _extract_text_file(fp: Path) -> str:
    data = fp.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _extract_pdf(fp: Path) -> list[str]:
    """PDF → 按页文本列表。优先 vendor 内 pdfminer，缺失时抛 ValueError（降级提示）。"""
    try:
        from pdfminer.high_level import extract_text  # type: ignore
    except ImportError:
        raise ValueError(
            "PDF 解析需要 pdfminer.six：请放入 vendor/ 目录，或将教材转为 md/txt 再摄取"
        )
    return [p for p in (extract_text(str(fp)) or "").split("\n\n") if p.strip()]


def _split_chunks(text: str, page: int = 0) -> list[tuple[int, str]]:
    """段落聚合分块：约 500 字/块、重叠 60 字。返回 [(page, content)]。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[tuple[int, str]] = []
    buf = ""
    for para in paras:
        if len(buf) + len(para) + 1 <= 500:
            buf = (buf + "\n" + para) if buf else para
        else:
            if buf:
                chunks.append((page, buf))
            if len(para) > 500:
                start = 0
                while start < len(para):
                    chunks.append((page, para[start:start + 500]))
                    start += 500
                buf = ""
            else:
                buf = para
    if buf:
        chunks.append((page, buf))
    # 相邻块首尾重叠 60 字，保证切分处语义连续
    if len(chunks) <= 1:
        return chunks
    merged: list[tuple[int, str]] = [chunks[0]]
    for i in range(1, len(chunks)):
        pg, content = chunks[i]
        prev_pg, prev = merged[-1]
        if pg == prev_pg:
            overlap = content[:60] if len(prev) >= 60 else content
            merged[-1] = (prev_pg, prev + "\n" + overlap)
        merged.append((pg, content))
    return merged


def ingest_path(path: str) -> dict[str, Any]:
    """摄取单个文件或目录（仅工作区内）。返回统计。"""
    fp = _safe_relative(path)
    if not fp:
        raise ValueError("路径必须在工作区内（不允许越界访问）")
    if fp.is_dir():
        return _ingest_dir(fp)
    if not fp.is_file():
        raise ValueError(f"路径不存在: {path}")
    return _ingest_file(fp)


def _ingest_dir(d: Path) -> dict[str, Any]:
    stats = {"docs": 0, "chunks": 0, "errors": []}
    for child in sorted(d.rglob("*")):
        if not child.is_file():
            continue
        if child.suffix.lower() not in (".md", ".txt", ".pdf"):
            continue
        try:
            s = _ingest_file(child)
            stats["docs"] += 1
            stats["chunks"] += s["chunks"]
        except ValueError as exc:
            stats["errors"].append(str(child.name) + ": " + str(exc))
    return stats


def _ingest_file(fp: Path) -> dict[str, Any]:
    ext = fp.suffix.lower()
    if ext == ".pdf":
        pages_text = _extract_pdf(fp)
        pages = len(pages_text)
        chunks: list[tuple[int, str]] = []
        for pg, text in enumerate(pages_text, start=1):
            chunks.extend(_split_chunks(text, pg))
    else:
        chunks = _split_chunks(_extract_text_file(fp))
        pages = 0
    if not chunks:
        raise ValueError("未提取到可索引文本")
    with DB_LOCK, db() as conn:
        doc = row("SELECT id FROM rag_docs WHERE source_path = ?", (str(fp),))
        if doc:
            conn.execute("DELETE FROM rag_chunks WHERE doc_id = ?", (doc["id"],))
            doc_id = doc["id"]
            conn.execute(
                "UPDATE rag_docs SET file_type = ?, pages = ?, chunk_count = ?, ingested_at = ? WHERE id = ?",
                (ext, pages, len(chunks), now(), doc_id),
            )
        else:
            cur = conn.execute(
                "INSERT INTO rag_docs(source_path, file_type, pages, chunk_count, ingested_at) VALUES (?, ?, ?, ?, ?)",
                (str(fp), ext, pages, len(chunks), now()),
            )
            doc_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO rag_chunks(doc_id, chunk_index, page, content) VALUES (?, ?, ?, ?)",
            [(doc_id, i, pg, content) for i, (pg, content) in enumerate(chunks)],
        )
        _sync_fts(conn, doc_id)
        _invalidate_bm25()
        # M8：向量化（可选）；失败仅告警，不阻断 BM25 检索
        model = _current_embedding_model()
        if model:
            try:
                _embed_and_store(conn, doc_id, model)
            except Exception as exc:
                LOG.warning("向量嵌入失败（降级纯 BM25）: %s", exc)
    LOG.info("RAG 摄取: %s（%d 块）", fp.name, len(chunks))
    return {"doc": str(fp), "chunks": len(chunks), "pages": pages}


def _fts_available() -> bool:
    with db() as conn:
        r = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rag_fts'"
        ).fetchone()
        return bool(r)


def _sync_fts(conn: Any, doc_id: int) -> None:
    try:
        conn.execute("DELETE FROM rag_fts WHERE rowid IN (SELECT id FROM rag_chunks WHERE doc_id = ?)", (doc_id,))
        conn.execute(
            "INSERT INTO rag_fts(rowid, content) SELECT id, content FROM rag_chunks WHERE doc_id = ?",
            (doc_id,),
        )
    except Exception as exc:
        LOG.warning("FTS5 同步失败（降级 BM25）: %s", exc)


def list_docs() -> list[dict[str, Any]]:
    return rows("SELECT * FROM rag_docs ORDER BY ingested_at DESC")


_UNDO: dict[int, dict[str, Any]] = {}
"""删除快照：doc_id -> {doc, chunks}，供前端撤销误删（内存态，进程内有效）。"""


def delete_doc(doc_id: int) -> bool:
    with DB_LOCK, db() as conn:
        doc = row("SELECT * FROM rag_docs WHERE id = ?", (doc_id,))
        if doc is None:
            return False
        chunks = rows("SELECT chunk_index, page, content FROM rag_chunks WHERE doc_id = ? ORDER BY chunk_index", (doc_id,))
        conn.execute("DELETE FROM rag_embeddings WHERE chunk_id IN (SELECT id FROM rag_chunks WHERE doc_id = ?)", (doc_id,))
        conn.execute("DELETE FROM rag_chunks WHERE doc_id = ?", (doc_id,))
        cur = conn.execute("DELETE FROM rag_docs WHERE id = ?", (doc_id,))
        _invalidate_bm25()
        if cur.rowcount > 0:
            _UNDO[doc_id] = {"doc": doc, "chunks": chunks}
        return cur.rowcount > 0


def restore_doc(doc_id: int) -> bool:
    """撤销删除：恢复 rag_docs + rag_chunks（同 id）。"""
    snap = _UNDO.pop(doc_id, None)
    if not snap:
        return False
    doc = snap["doc"]
    with DB_LOCK, db() as conn:
        try:
            conn.execute(
                "INSERT INTO rag_docs(id, source_path, file_type, pages, chunk_count, ingested_at) VALUES (?, ?, ?, ?, ?, ?)",
                (doc["id"], doc["source_path"], doc["file_type"], doc["pages"], doc["chunk_count"], doc["ingested_at"]),
            )
        except Exception:
            return False
        conn.executemany(
            "INSERT INTO rag_chunks(doc_id, chunk_index, page, content) VALUES (?, ?, ?, ?)",
            [(doc_id, c["chunk_index"], c["page"], c["content"]) for c in snap["chunks"]],
        )
        _sync_fts(conn, doc_id)
        _invalidate_bm25()
        # M8：撤销删除后尽力重建向量（失败不阻断，BM25 仍可用）
        model = _current_embedding_model()
        if model:
            try:
                _embed_and_store(conn, doc_id, model)
            except Exception as exc:
                LOG.warning("撤销删除后向量重建失败（BM25 仍可用）: %s", exc)
        return True


_BM25_CACHE: dict[str, Any] = {"docs": None}
"""C6：BM25 统计缓存（token 化各块词频 + 平均长度），摄取/删除后失效。"""


def _bm25_stats() -> tuple[list[dict[str, Any]], list[dict[str, int]], float]:
    """一次性计算并缓存块统计；块集合变化后由 _invalidate_bm25() 失效。"""
    cached = _BM25_CACHE.get("docs")
    if cached is not None:
        return cached
    chunks = rows("SELECT id, doc_id, page, content FROM rag_chunks")
    doc_lens = [_freq(_tokenize(c["content"])) for c in chunks]
    avg_len = max(1.0, sum(len(d) for d in doc_lens) / len(chunks)) if chunks else 1.0
    _BM25_CACHE["docs"] = (chunks, doc_lens, avg_len)
    return _BM25_CACHE["docs"]


def _invalidate_bm25() -> None:
    _BM25_CACHE["docs"] = None


def _bm25(query: str, k: int = 5) -> list[dict[str, Any]]:
    q_tokens = _freq(_tokenize(query))
    if not q_tokens:
        return []
    all_chunks, doc_lens, avg_len = _bm25_stats()
    if not all_chunks:
        return []
    n = len(all_chunks)
    idf: dict[str, float] = {}
    for t in q_tokens:
        df = sum(1 for d in doc_lens if t in d)
        idf[t] = math.log(1 + (n - df + 0.5) / (df + 0.5))
    k1, b = 1.5, 0.75
    scored: list[tuple[float, dict[str, Any]]] = []
    for chunk, freq in zip(all_chunks, doc_lens):
        dl = len(freq)
        score = 0.0
        for t, qf in q_tokens.items():
            tf = freq.get(t, 0)
            if tf:
                score += idf.get(t, 0.0) * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avg_len)) * qf
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda x: -x[0])
    return [
        {"chunk_id": c["id"], "doc_id": c["doc_id"], "page": c["page"],
         "content": c["content"][:300], "score": round(s, 4)}
        for s, c in scored[:k]
    ]


def _fts_search(query: str, k: int = 5) -> list[dict[str, Any]]:
    if not _fts_available():
        return []
    words = [t for t in _tokenize(query) if len(t) >= 2][:8]
    if not words:
        return []
    q = " OR ".join(f'"{w}"' for w in words)
    try:
        return rows("""
            SELECT c.id AS chunk_id, c.doc_id, c.page, substr(c.content, 1, 300) AS content,
                   round(bm25(rag_fts), 4) AS score
            FROM rag_fts JOIN rag_chunks c ON c.id = rag_fts.rowid
            WHERE rag_fts MATCH ? ORDER BY score LIMIT ?
        """, (q, k))
    except Exception as exc:
        LOG.warning("FTS5 查询失败（降级 BM25）: %s", exc)
        return []


def filter_relevant(hits: list[dict[str, Any]], ratio: float = 0.35) -> list[dict[str, Any]]:
    """按相对分阈值过滤检索结果（零 AI 调用，纯本地）。

    保留 score >= ratio * 最高分的命中：BM25 绝对分无跨语料可比性，
    相对分可剔除"沾边但低质"的长尾片段。最高分命中恒保留；空输入返回空。
    """
    if not hits:
        return []
    best = max(h.get("score", 0) for h in hits)
    if best <= 0:
        return hits[:1]
    return [h for h in hits if h.get("score", 0) >= ratio * best]


def search(query: str, k: int = 5) -> list[dict[str, Any]]:
    """BM25 主 + FTS5 可选；若启用向量检索则三路 RRF 融合。

    向量未启用或不可用时静默回落纯词法合并（与旧行为一致），保证零回归。
    """
    bm = _bm25(query, k)
    fts = _fts_search(query, k)
    if not embedding_enabled():
        # 维持现状：BM25 主 + FTS 可选，按 chunk 去重取更优分
        if not fts:
            return bm
        merged: dict[int, dict[str, Any]] = {}
        for item in bm + fts:
            cid = item["chunk_id"]
            cur = merged.get(cid)
            if cur is None or item.get("score", 0) > cur.get("score", 0):
                merged[cid] = item
        return sorted(merged.values(), key=lambda x: -x["score"])[:k]
    vec = _vector_search(query, k)
    fused = _rrf_merge([bm, fts, vec])
    return filter_relevant(fused)[:k]


# ── M8 可选向量检索（零第三方依赖；embedding_model 未配置则整条链路降级 BM25）──
_EMBED_BATCH = 16
_RRF_K = 60


def _current_embedding_model() -> str:
    """当前生效的 embedding 模型；空串表示未启用向量检索。"""
    try:
        from ai import get_cached_settings
        s = get_cached_settings()
        return (s.get("embedding_model", "") or "").strip() or (s.get("model", "") or "").strip()
    except Exception:
        return ""


def embedding_enabled() -> bool:
    return bool(_current_embedding_model())


def _embed_batch(texts: list[str], model: str) -> list[list[float]]:
    """批量向量化（每批 _EMBED_BATCH 条，控制请求数）。"""
    import ai
    out: list[list[float]] = []
    for i in range(0, len(texts), _EMBED_BATCH):
        out.extend(ai.embed(texts[i:i + _EMBED_BATCH], model=model))
    return out


def _embed_and_store(conn: Any, doc_id: int, model: str) -> None:
    """为某文档全部 chunk 生成并落库向量（替换旧向量）。失败抛异常由调用方降级。"""
    chs = rows("SELECT id, content FROM rag_chunks WHERE doc_id = ? ORDER BY chunk_index", (doc_id,))
    if not chs:
        return
    vecs = _embed_batch([c["content"] for c in chs], model)
    if not vecs or len(vecs) != len(chs):
        raise RuntimeError("embedding 返回数量与 chunk 不一致")
    conn.execute(
        "DELETE FROM rag_embeddings WHERE chunk_id IN (SELECT id FROM rag_chunks WHERE doc_id = ?)",
        (doc_id,),
    )
    packed = [
        (c["id"], len(v), model, struct.pack("<%df" % len(v), *v))
        for c, v in zip(chs, vecs)
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO rag_embeddings(chunk_id, dim, model, vec) VALUES (?, ?, ?, ?)",
        packed,
    )


def _unpack_vec(blob: Any, dim: int) -> list[float] | None:
    if blob is None:
        return None
    try:
        return list(struct.unpack("<%df" % dim, blob))
    except Exception:
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _vector_search(query: str, k: int) -> list[dict[str, Any]]:
    """查询向量化 + 与当前模型向量库余弦排序取 top；空模型/失败返回 []。

    仅检索与当前 embedding_model 同名的向量，避免维度不一致的模型混用导致崩溃。
    """
    model = _current_embedding_model()
    if not model:
        return []
    try:
        import ai
        qv = ai.embed([query], model=model)[0]
    except Exception as exc:
        LOG.warning("查询向量化失败（降级纯词法）: %s", exc)
        return []
    recs = rows("SELECT chunk_id, dim, vec FROM rag_embeddings WHERE model = ?", (model,))
    scored: list[tuple[float, int]] = []
    for r in recs:
        v = _unpack_vec(r["vec"], r["dim"])
        if v is None:
            continue
        scored.append((_cosine(qv, v), r["chunk_id"]))
    if not scored:
        return []
    scored.sort(key=lambda x: -x[0])
    top = scored[: k * 3]
    ids = [cid for _, cid in top]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    ch = rows(f"SELECT id, doc_id, page, content FROM rag_chunks WHERE id IN ({placeholders})", ids)
    cmap = {c["id"]: c for c in ch}
    out: list[dict[str, Any]] = []
    for sim, cid in top:
        c = cmap.get(cid)
        if not c:
            continue
        out.append({
            "chunk_id": cid, "doc_id": c["doc_id"], "page": c["page"],
            "content": c["content"][:300], "score": round(sim, 4),
        })
    return out


def _rrf_merge(ranked_lists: list[list[dict[str, Any]]], k: int = _RRF_K) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion：多路召回按排名融合，弱化各路绝对分差异。"""
    acc: dict[int, dict[str, Any]] = {}
    for lst in ranked_lists:
        for rank, item in enumerate(lst):
            cid = item["chunk_id"]
            entry = acc.get(cid)
            if entry is None:
                entry = {"item": item, "s": 0.0}
                acc[cid] = entry
            entry["s"] += 1.0 / (k + rank + 1)
    merged = [e["item"] for e in acc.values()]
    for e in merged:
        e["score"] = round(acc[e["chunk_id"]]["s"], 6)
    merged.sort(key=lambda x: -x["score"])
    return merged


def registered_paths() -> set[str]:
    return {r["source_path"] for r in rows("SELECT source_path FROM rag_docs")}
