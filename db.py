"""数据访问层：SQLite 连接管理与通用查询。"""
from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from config import DB_PATH, SCHEMA, DEFAULT_SETTINGS, LOG

# RLock（可重入）：消除"持锁中再次请求锁"潜在的死锁隐患；对现有串行语义无改变。
DB_LOCK = threading.RLock()

# R4：线程本地连接复用。ThreadingHTTPServer 每请求一线程，连接随线程缓存，
# 省去每次 `db()` 重新 connect + 重跑 9 条 PRAGMA 的开销；`check_same_thread=False`
# 允许跨线程回收（配合线程结束自然关闭，见 `close_thread_conn`）。
_TLS = threading.local()

# 全局连接登记表 + 代次：文件级备份/还原前 `close_all_connections()` 关闭全部线程
# 的连接（否则 Windows 上文件被占用 rename/copy 失败）；epoch 递增使各线程下次
# 取连接时自动重建，避免复用已关闭的连接。
#
# 结构为 {threading.get_ident(): conn}：
#   早期用 set（强引用）持有连接，注释称「配合线程结束自然关闭」并不成立——
#   set 的强引用让连接在线程死亡后仍被钉住，永不释放（sqlite3.Connection 不可
#   weakref，无法用 WeakSet 兜底）。HTTP keep-alive 每条约 120s 回收一次线程，
#   每个死线程留下一个打开句柄（含 64MB 页缓存），Windows 上还长期占用
#   learnos.db / -wal 文件句柄。
#   改为按线程 ident 建索引：ident 被新线程复用时旧条目自动被覆盖（旧连接随之释放），
#   并由 _reap_dead_conns() 定期回收已退出的线程。
_ALL_CONNS: dict[int, sqlite3.Connection] = {}
_ALL_CONNS_LOCK = threading.Lock()
_EPOCH = 0
# 登记表容量软上限：超过则触发一次死线程连接回收（见 _reap_dead_conns）。
_CONN_REAP_THRESHOLD = 8


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# 生产级 PRAGMA（§1.3/§16.1）：在 journal_mode=WAL 之后统一追加。
# mmap_size 在某些沙箱/文件系统中不可用，单独 try 避免连接失败。
_PROD_PRAGMAS = [
    "PRAGMA busy_timeout = 5000",          # 写竞争时等待而非立即报错
    "PRAGMA synchronous = NORMAL",         # WAL 下安全且显著降低 fsync 开销
    # 16MB 页缓存（负值为 KB）。原为 64MB，但库本体仅约 14MB，且缓存是**每连接**
    # 独立的——多线程叠加时（每请求一线程 + keep-alive）会成倍放大内存占用。
    "PRAGMA cache_size = -16000",
    "PRAGMA temp_store = MEMORY",          # 临时表/索引常驻内存
    "PRAGMA wal_autocheckpoint = 1000",    # 每 1000 页自动 checkpoint
    # WAL 文件体积上限（字节，64MB）：防止在两次 autocheckpoint 之间的高频写入
    # 让 -wal 文件无限膨胀。达到上限后 SQLite 会在下次事务时强制 checkpoint。
    # 库本体约 14MB，64MB 上限留有足够缓冲又不至于失控（超出会触发 checkpoint）。
    "PRAGMA journal_size_limit = 67108864",
    "PRAGMA secure_delete = OFF",          # 非隐私删除场景，关掉安全擦除提性能
]


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    for stmt in _PROD_PRAGMAS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            # 个别 PRAGMA（如 mmap_size 受限）在特殊文件系统下不可设，忽略不影响正确性
            LOG.warning("PRAGMA 未生效（忽略）: %s", stmt)
    try:
        # 256MB 内存映射：大幅提升大库读取吞吐；受限环境静默跳过
        conn.execute("PRAGMA mmap_size = 268435456")
    except sqlite3.OperationalError:
        LOG.debug("PRAGMA mmap_size 不可用，跳过")
    return conn


def _thread_conn() -> sqlite3.Connection:
    """取当前线程的本地连接（首次创建并缓存；测试重绑 DB_PATH 或全连接关闭后自动重建）。"""
    conn = getattr(_TLS, "conn", None)
    path = getattr(_TLS, "db_path", None)
    epoch = getattr(_TLS, "epoch", -1)
    if conn is None or path != str(DB_PATH) or epoch != _EPOCH:
        # DB_PATH 变化（测试切临时库）、线程首用、或 close_all_connections 后：旧连接作废，重建
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        conn = connect()
        _TLS.conn = conn
        _TLS.db_path = str(DB_PATH)
        _TLS.epoch = _EPOCH
        with _ALL_CONNS_LOCK:
            ident = threading.get_ident()
            # ident 被新线程复用 → 旧线程确已退出，显式关闭其连接而非留给 GC：
            # 否则会产生 ResourceWarning: unclosed database，且 WAL checkpoint
            # 时机不可控（GC 可能在整库备份/还原的临界点才触发）。
            old = _ALL_CONNS.get(ident)
            if old is not None and old is not conn:
                try:
                    old.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
                try:
                    old.close()
                except sqlite3.Error:
                    pass
            _ALL_CONNS[ident] = conn
            if len(_ALL_CONNS) > _CONN_REAP_THRESHOLD:
                _reap_dead_conns()
    return conn


def _reap_dead_conns() -> int:
    """关闭并移除「所属线程已退出」的连接。返回回收数量。

    调用方须已持有 _ALL_CONNS_LOCK。
    """
    live = {t.ident for t in threading.enumerate()}
    dead = [ident for ident in _ALL_CONNS if ident not in live]
    for ident in dead:
        conn = _ALL_CONNS.pop(ident, None)
        if conn is None:
            continue
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        try:
            conn.close()
        except sqlite3.Error:
            pass
    if dead:
        LOG.info("回收 %d 个已退出线程残留的数据库连接", len(dead))
    return len(dead)


def close_thread_conn() -> None:
    """关闭当前线程的本地连接（文件级备份/还原前调用）。

    连接复用后打开的连接会：① 锁住库文件导致 Windows rename/copy 失败；
    ② 未 checkpoint 的 WAL 数据仍在 -wal 文件中，直接拷 .db 会漏数据。
    """
    if getattr(_TLS, "depth", 0):
        # 嵌套 db() 内关闭连接会让最外层 with conn: 对已关连接 commit 抛错，且 finally 会掩盖深度失衡
        raise RuntimeError("close_thread_conn 不能在嵌套 db() 事务内调用（depth != 0）")
    conn = getattr(_TLS, "conn", None)
    if conn is not None:
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        try:
            conn.close()
        except sqlite3.Error:
            pass
        with _ALL_CONNS_LOCK:
            _ALL_CONNS.pop(threading.get_ident(), None)
    _TLS.conn = None
    _TLS.db_path = None
    _TLS.depth = 0


def close_all_connections() -> None:
    """关闭所有线程的本地连接（整库还原前调用）。

    还原需 rename 现库文件，任何线程的打开连接都会在 Windows 上锁文件；
    同时 WAL 须 checkpoint 落盘。关闭后递增 epoch，各线程下次取连接自动重建。
    """
    global _EPOCH
    with _ALL_CONNS_LOCK:
        conns = list(_ALL_CONNS.values())
        _ALL_CONNS.clear()
    for c in conns:
        try:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        try:
            c.close()
        except sqlite3.Error:
            pass
    _EPOCH += 1


@contextmanager
def db():
    conn = _thread_conn()
    depth = getattr(_TLS, "depth", 0)
    if depth == 0:
        # 最外层：负责事务边界（with conn 提交/回滚）
        _TLS.depth = 1
        try:
            with conn:
                yield conn
        finally:
            _TLS.depth = 0
    else:
        # 嵌套 db()（如 handler 内调 rows()/row() 内部又开 db()）：
        # 复用同一连接但不再套 `with conn`，避免内层提前 commit 破坏外层事务原子性。
        _TLS.depth = depth + 1
        try:
            yield conn
        finally:
            _TLS.depth = depth


def _v31_normalize_concept_ids(conn: sqlite3.Connection) -> int:
    """v31：把 problems.concept_ids 的历史脏值统一为 ',1,7,'。返回清洗行数。

    三种历史格式（'[]' / ',1,,,7,' / ',1,7,'）—— split+isdigit 已能容错读出，
    故这里只是消除不一致：任何精确等值匹配、字符串长度假设、去重假设才可靠。
    """
    changed = 0
    try:
        rows = conn.execute(
            "SELECT id, concept_ids FROM problems WHERE concept_ids IS NOT NULL").fetchall()
    except sqlite3.Error as exc:  # 列不存在（理论不可能，v10 已建列）亦不阻断启动
        LOG.warning("v31 读取 concept_ids 失败，跳过清洗: %s", exc)
        return 0
    for r in rows:
        raw = r["concept_ids"]
        ids = [int(x) for x in (raw or "").split(",") if x.strip().isdigit()]
        seen: set[int] = set()
        ordered: list[int] = []
        for i in ids:  # 去重且保序
            if i not in seen:
                seen.add(i)
                ordered.append(i)
        normalized = f",{','.join(str(i) for i in ordered)}," if ordered else ""
        if normalized != raw:
            conn.execute("UPDATE problems SET concept_ids = ? WHERE id = ?",
                         (normalized, r["id"]))
            changed += 1
    if changed:
        LOG.info("v31 已清洗 %d 行 concept_ids 格式", changed)
    return changed


def _migrate(conn: sqlite3.Connection) -> None:
    """版本化数据库迁移：每次 schema 变更新增一个版本条目。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row and row[0] is not None else 0

    # v1: v0.1 → v0.2 — 添加 ease_factor 和 repetition 列
    if current < 1:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(problems)").fetchall()}
        if "ease_factor" not in cols:
            conn.execute("ALTER TABLE problems ADD COLUMN ease_factor REAL NOT NULL DEFAULT 2.5")
        if "repetition" not in cols:
            conn.execute("ALTER TABLE problems ADD COLUMN repetition INTEGER NOT NULL DEFAULT 0")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (1, ?)", (now(),))
        LOG.info("数据库已迁移到 v1 (添加 ease_factor/repetition)")

    # v2: 掌握度趋势日志表
    if current < 2:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mastery_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                avg_mastery REAL NOT NULL,
                count INTEGER NOT NULL
            )
        """)
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (2, ?)", (now(),))
        LOG.info("数据库已迁移到 v2 (添加 mastery_log)")

    # v3: 收藏/星标列
    if current < 3:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(problems)").fetchall()}
        if "starred" not in cols:
            conn.execute("ALTER TABLE problems ADD COLUMN starred INTEGER NOT NULL DEFAULT 0")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (3, ?)", (now(),))
        LOG.info("数据库已迁移到 v3 (添加 starred)")

    # v4: R4 合规 — 密钥出库。清除 settings 表中的 api_key 行（若有旧版残留），
    # 密钥改由 keystore 管理（keys.enc / 环境变量 / 内存）。
    if current < 4:
        conn.execute("DELETE FROM settings WHERE key = 'api_key'")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (4, ?)", (now(),))
        LOG.info("数据库已迁移到 v4 (密钥出库，清除 settings.api_key)")

    # v5: A3 错因结构化 — 新增错因画像字段
    if current < 5:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(problems)").fetchall()}
        for col in ("error_path", "trap_note", "shortcut", "fix_action"):
            if col not in cols:
                conn.execute(f"ALTER TABLE problems ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (5, ?)", (now(),))
        LOG.info("数据库已迁移到 v5 (错因结构化字段 error_path/trap_note/shortcut/fix_action)")

    # v6: A1 FSRS 调度状态 — 持久化 state/stability/difficulty（SM-2 字段保留兼容）
    if current < 6:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(problems)").fetchall()}
        # 固定 SQL 白名单，不与变量拼接
        if "state" not in cols:
            conn.execute("ALTER TABLE problems ADD COLUMN state REAL NOT NULL DEFAULT 0")
        if "stability" not in cols:
            conn.execute("ALTER TABLE problems ADD COLUMN stability REAL NOT NULL DEFAULT 0")
        if "difficulty" not in cols:
            conn.execute("ALTER TABLE problems ADD COLUMN difficulty REAL NOT NULL DEFAULT 0")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (6, ?)", (now(),))
        LOG.info("数据库已迁移到 v6 (FSRS 状态列 state/stability/difficulty)")

    # v7: B5 自动标签 + 知识提取 — 已确认标签 / AI 草稿（R3 不静默落库）/ 状态标记
    if current < 7:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(problems)").fetchall()}
        if "tags" not in cols:
            conn.execute("ALTER TABLE problems ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
        if "tags_suggested" not in cols:
            conn.execute("ALTER TABLE problems ADD COLUMN tags_suggested TEXT NOT NULL DEFAULT ''")
        if "tags_status" not in cols:
            conn.execute("ALTER TABLE problems ADD COLUMN tags_status TEXT NOT NULL DEFAULT 'none'")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (7, ?)", (now(),))
        LOG.info("数据库已迁移到 v7 (B5 标签列 tags/tags_suggested/tags_status)")

    # v8: C5 学习者档案 — 偏好/目标键值表（画像聚合实时计算，不落缓存）
    if current < 8:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS learner_profile (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (8, ?)", (now(),))
        LOG.info("数据库已迁移到 v8 (学习者档案表 learner_profile)")

    # v9: A4 变式题引擎 — problems.variants(JSON) + reviews.variant_id（变式正确率回写）
    if current < 9:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(problems)").fetchall()}
        if "variants" not in cols:
            conn.execute("ALTER TABLE problems ADD COLUMN variants TEXT NOT NULL DEFAULT '[]'")
        rcols = {r[1] for r in conn.execute("PRAGMA table_info(reviews)").fetchall()}
        if "variant_id" not in rcols:
            conn.execute("ALTER TABLE reviews ADD COLUMN variant_id INTEGER NOT NULL DEFAULT 0")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (9, ?)", (now(),))
        LOG.info("数据库已迁移到 v9 (A4 变式题 variants/variant_id)")

    # v10: A2 概念知识图谱 — concepts/concept_links/concept_progress + problems.concept_ids
    if current < 10:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS concepts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                parent_id INTEGER NOT NULL DEFAULT 0,
                chapter_id INTEGER NOT NULL DEFAULT 0,
                difficulty REAL NOT NULL DEFAULT 0.5,
                mastery_est REAL NOT NULL DEFAULT 0.0,
                looms_in INTEGER NOT NULL DEFAULT 0,
                user_edited INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS concept_links (
                concept_a INTEGER NOT NULL,
                concept_b INTEGER NOT NULL,
                relation TEXT NOT NULL CHECK (relation IN ('prerequisite', 'related', 'contrast')),
                PRIMARY KEY (concept_a, concept_b, relation)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS concept_progress (
                concept_id INTEGER PRIMARY KEY,
                mastery REAL NOT NULL DEFAULT 0.0,
                reviews INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
        """)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(problems)").fetchall()}
        if "concept_ids" not in cols:
            conn.execute("ALTER TABLE problems ADD COLUMN concept_ids TEXT NOT NULL DEFAULT '[]'")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (10, ?)", (now(),))
        LOG.info("数据库已迁移到 v10 (A2 知识图谱 concepts/concept_links/concept_progress/concept_ids)")

    # v11: A5 Feynman 口述反转 — oral_sessions 加模式/关联题目/自评表
    if current < 11:
        ocols = {r[1] for r in conn.execute("PRAGMA table_info(oral_sessions)").fetchall()}
        if "mode" not in ocols:
            conn.execute("ALTER TABLE oral_sessions ADD COLUMN mode TEXT NOT NULL DEFAULT 'socratic'")
        if "problem_id" not in ocols:
            conn.execute("ALTER TABLE oral_sessions ADD COLUMN problem_id INTEGER NOT NULL DEFAULT 0")
        if "self_review" not in ocols:
            conn.execute("ALTER TABLE oral_sessions ADD COLUMN self_review TEXT NOT NULL DEFAULT ''")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (11, ?)", (now(),))
        LOG.info("数据库已迁移到 v11 (A5 Feynman mode/problem_id/self_review)")

    # v12: B1 拍照/截图录题 — 题目图片附件列
    if current < 12:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(problems)").fetchall()}
        if "media_path" not in cols:
            conn.execute("ALTER TABLE problems ADD COLUMN media_path TEXT NOT NULL DEFAULT ''")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (12, ?)", (now(),))
        LOG.info("数据库已迁移到 v12 (B1 拍照 media_path)")

    # v13: B3 个人资料 RAG — 教材/课件/笔记摄取与分块
    if current < 13:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rag_docs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL UNIQUE,
                file_type TEXT NOT NULL DEFAULT '',
                pages INTEGER NOT NULL DEFAULT 0,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                ingested_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rag_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL DEFAULT 0,
                page INTEGER NOT NULL DEFAULT 0,
                content TEXT NOT NULL,
                FOREIGN KEY(doc_id) REFERENCES rag_docs(id) ON DELETE CASCADE
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rag_chunks_doc ON rag_chunks(doc_id)")
        try:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5(content, tokenize='unicode61')")
        except sqlite3.OperationalError:
            LOG.warning("FTS5 不可用，RAG 检索将仅使用 BM25（可选降级）")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (13, ?)", (now(),))
        LOG.info("数据库已迁移到 v13 (B3 RAG rag_docs/rag_chunks)")

    # v14: B4 真题对齐 + 考试就绪度
    if current < 14:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS exam_papers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                exam_date TEXT NOT NULL DEFAULT '',
                target REAL NOT NULL DEFAULT 80,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS exam_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id INTEGER NOT NULL,
                qno TEXT NOT NULL DEFAULT '',
                topic TEXT NOT NULL DEFAULT '',
                weight REAL NOT NULL DEFAULT 1,
                content TEXT NOT NULL DEFAULT '',
                related_problems TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                FOREIGN KEY(paper_id) REFERENCES exam_papers(id) ON DELETE CASCADE
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_exam_q_paper ON exam_questions(paper_id)")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (14, ?)", (now(),))
        LOG.info("数据库已迁移到 v14 (B4 真题 exam_papers/exam_questions)")

    # v15: P0 批次 — AI 遥测 / 游戏化 / 一题多解（全部零依赖）
    if current < 15:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                route TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                latency_ms INTEGER NOT NULL DEFAULT 0,
                tokens INTEGER NOT NULL DEFAULT 0,
                ok INTEGER NOT NULL DEFAULT 0,
                error_kind TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON ai_telemetry(ts)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gamification (
                date TEXT PRIMARY KEY,
                reviews INTEGER NOT NULL DEFAULT 0,
                xp INTEGER NOT NULL DEFAULT 0
            )
        """)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(problems)").fetchall()}
        if "methods" not in cols:
            conn.execute("ALTER TABLE problems ADD COLUMN methods TEXT NOT NULL DEFAULT '[]'")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (15, ?)", (now(),))
        LOG.info("数据库已迁移到 v15 (ai_telemetry/gamification/problems.methods)")

    # v16: 题库 — 答题记录 bank_attempts / 题库-错题映射 bank_problems
    if current < 16:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bank_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                qid TEXT NOT NULL,
                correct INTEGER NOT NULL,
                attempted_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bank_problems (
                qid TEXT PRIMARY KEY,
                problem_id INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bank_attempts_qid ON bank_attempts(qid, id)")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (16, ?)", (now(),))
        LOG.info("数据库已迁移到 v16 (题库 bank_attempts/bank_problems)")

    # v17: 多学科 — concepts/problems/bank 相关表加 subject 列，现有数据归 physics
    if current < 17:
        ccols = {r[1] for r in conn.execute("PRAGMA table_info(concepts)").fetchall()}
        if "subject" not in ccols:
            conn.execute("ALTER TABLE concepts ADD COLUMN subject TEXT NOT NULL DEFAULT 'physics'")
        pcols = {r[1] for r in conn.execute("PRAGMA table_info(problems)").fetchall()}
        if "subject" not in pcols:
            conn.execute("ALTER TABLE problems ADD COLUMN subject TEXT NOT NULL DEFAULT 'physics'")
        bcols = {r[1] for r in conn.execute("PRAGMA table_info(bank_problems)").fetchall()}
        if "subject" not in bcols:
            conn.execute("ALTER TABLE bank_problems ADD COLUMN subject TEXT NOT NULL DEFAULT 'physics'")
        mcols = {r[1] for r in conn.execute("PRAGMA table_info(mastery_log)").fetchall()}
        if "subject" not in mcols:
            conn.execute("ALTER TABLE mastery_log ADD COLUMN subject TEXT NOT NULL DEFAULT 'physics'")
        # concepts.name 的 UNIQUE 约束改为 (subject, name) 复合唯一，允许跨学科同名概念
        uniq = [r[1] for r in conn.execute("PRAGMA index_list(concepts)").fetchall()]
        if "sqlite_autoindex_concepts_1" in uniq:
            conn.execute("ALTER TABLE concepts RENAME TO concepts_old")
            conn.execute("""
                CREATE TABLE concepts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    parent_id INTEGER NOT NULL DEFAULT 0,
                    chapter_id INTEGER NOT NULL DEFAULT 0,
                    difficulty REAL NOT NULL DEFAULT 0.5,
                    mastery_est REAL NOT NULL DEFAULT 0.0,
                    looms_in INTEGER NOT NULL DEFAULT 0,
                    user_edited INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    subject TEXT NOT NULL DEFAULT 'physics',
                    UNIQUE (subject, name)
                )
            """)
            conn.execute("""
                INSERT INTO concepts(id, name, parent_id, chapter_id, difficulty,
                                     mastery_est, looms_in, user_edited, created_at, subject)
                SELECT id, name, parent_id, chapter_id, difficulty,
                       mastery_est, looms_in, user_edited, created_at, subject FROM concepts_old
            """)
            conn.execute("DROP TABLE concepts_old")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_concepts_subject ON concepts(subject, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_problems_subject ON problems(subject, id)")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (17, ?)", (now(),))
        LOG.info("数据库已迁移到 v17 (多学科 subject 列 + 复合唯一)")

    # v18: 学科注册表 — 内置三科 + 种子文件学科自动注册，网页端可增删自建学科
    if current < 18:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subjects (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                builtin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (18, ?)", (now(),))
        LOG.info("数据库已迁移到 v18 (学科注册表 subjects)")

    # v19: 概念别名 — 支持未链接提及扫描的别名匹配（如 N2L = 牛顿第二定律）
    if current < 19:
        ccols = {r[1] for r in conn.execute("PRAGMA table_info(concepts)").fetchall()}
        if "aliases" not in ccols:
            conn.execute("ALTER TABLE concepts ADD COLUMN aliases TEXT NOT NULL DEFAULT ''")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (19, ?)", (now(),))
        LOG.info("数据库已迁移到 v19 (概念别名 concepts.aliases)")

    # v20: AI 遥测缓存命中 — 记录 prompt 缓存命中 token（DeepSeek/OpenAI 自动缓存可观测）
    if current < 20:
        tcols = {r[1] for r in conn.execute("PRAGMA table_info(ai_telemetry)").fetchall()}
        if "cached_tokens" not in tcols:
            conn.execute("ALTER TABLE ai_telemetry ADD COLUMN cached_tokens INTEGER NOT NULL DEFAULT 0")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (20, ?)", (now(),))
        LOG.info("数据库已迁移到 v20 (ai_telemetry.cached_tokens)")

    # v21: 学习小组本地优先打卡（§34.2/§42.3）— 问责/连续天数，不暴露任何题目或答案。
    if current < 21:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS study_checkins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                check_date TEXT NOT NULL,
                subject TEXT NOT NULL DEFAULT '',
                minutes INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_checkins_date ON study_checkins(check_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_checkins_subject ON study_checkins(subject)")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (21, ?)", (now(),))
        LOG.info("数据库已迁移到 v21 (学习小组打卡 study_checkins)")

    # v22: 题库 AI 评分历史 — bank_scores（主观题/大小题 AI 评分结果持久化）
    if current < 22:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bank_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                qid TEXT NOT NULL,
                subject TEXT NOT NULL DEFAULT '',
                score INTEGER,
                comment TEXT NOT NULL DEFAULT '',
                against TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL DEFAULT 'unrated',
                needs_review INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bank_scores_qid ON bank_scores(qid)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bank_scores_qid_ts ON bank_scores(qid, created_at)")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (22, ?)", (now(),))
        LOG.info("数据库已迁移到 v22 (题库 AI 评分历史 bank_scores)")

    # v23: 概念详解 — concepts.explanation 支持手写概念释义（离线可用，AI 可辅助生成）
    if current < 23:
        ccols = {r[1] for r in conn.execute("PRAGMA table_info(concepts)").fetchall()}
        if "explanation" not in ccols:
            conn.execute("ALTER TABLE concepts ADD COLUMN explanation TEXT NOT NULL DEFAULT ''")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (23, ?)", (now(),))
        LOG.info("数据库已迁移到 v23 (concepts.explanation 概念详解)")

    # v24: 概念来源标记 + 种子版本表 — 区分 seed/import/ai/rag，支持种子升级提示
    if current < 24:
        ccols = {r[1] for r in conn.execute("PRAGMA table_info(concepts)").fetchall()}
        if "source" not in ccols:
            conn.execute("ALTER TABLE concepts ADD COLUMN source TEXT NOT NULL DEFAULT 'unknown'")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS seed_versions (
                subject TEXT PRIMARY KEY,
                seed_version INTEGER NOT NULL DEFAULT 0,
                applied_at TEXT NOT NULL
            )
        """)
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (24, ?)", (now(),))
        LOG.info("数据库已迁移到 v24 (concepts.source + seed_versions)")

    # v25: 概念闪卡（主动回忆）— 卡片 + 独立评分日志，与 problems/reviews 解耦
    #      卡片自带 FSRS 调度状态（复用 fsrs_bridge），不污染题目掌握度统计。
    if current < 25:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL DEFAULT 'physics',
                concept_id INTEGER NOT NULL DEFAULT 0,
                kind TEXT NOT NULL DEFAULT 'qa',
                cue TEXT NOT NULL,
                answer TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                source TEXT NOT NULL DEFAULT 'manual',
                created_at TEXT NOT NULL,
                ease_factor REAL NOT NULL DEFAULT 2.5,
                repetition INTEGER NOT NULL DEFAULT 0,
                state INTEGER NOT NULL DEFAULT 0,
                stability REAL NOT NULL DEFAULT 0.0,
                difficulty REAL NOT NULL DEFAULT 0.0,
                due_date TEXT NOT NULL DEFAULT '',
                interval_days INTEGER NOT NULL DEFAULT 1,
                last_review TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS card_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id INTEGER NOT NULL,
                due_date TEXT NOT NULL DEFAULT '',
                interval_days INTEGER NOT NULL DEFAULT 1,
                rating INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(card_id) REFERENCES cards(id) ON DELETE CASCADE
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_subject_due ON cards(subject, status, due_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_concept ON cards(concept_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_card_reviews_card ON card_reviews(card_id)")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (25, ?)", (now(),))
        LOG.info("数据库已迁移到 v25 (概念闪卡 cards/card_reviews)")

    # v26: 边关系扩展 3 → 6 种（先修/演进/包含/类比/易混/相关）— 重建 concept_links 的 CHECK
    if current < 26:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='concept_links'").fetchone()
        if sql and "analogy" not in str(sql["sql"]):
            conn.execute("ALTER TABLE concept_links RENAME TO concept_links_old")
            conn.execute("""
                CREATE TABLE concept_links (
                    concept_a INTEGER NOT NULL,
                    concept_b INTEGER NOT NULL,
                    relation TEXT NOT NULL CHECK (relation IN
                        ('prerequisite','related','contrast','analogy','inclusion','progression')),
                    PRIMARY KEY (concept_a, concept_b, relation)
                )
            """)
            conn.execute("INSERT INTO concept_links(concept_a, concept_b, relation) "
                         "SELECT concept_a, concept_b, relation FROM concept_links_old")
            conn.execute("DROP TABLE concept_links_old")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (26, ?)", (now(),))
        LOG.info("数据库已迁移到 v26 (边关系扩展为 6 种 concept_links)")

    # v27: 概念详解分层 — 种子基线(explanation_seed) + 用户覆盖层(explanation_user)，
    #      显示值 explanation 恒等于 COALESCE(explanation_user, explanation_seed)。
    #      种子加载(apply/ensure_seed)只写 explanation_seed，不触碰 explanation_user；
    #      用户保存只写 explanation_user；回档即清空 explanation_user 落回种子基线。
    #      如此区分后，重跑 apply 不会冲掉用户编辑（免费修复克隆风险），且支持单概念一键回档。
    if current < 27:
        ccols = {r[1] for r in conn.execute("PRAGMA table_info(concepts)").fetchall()}
        if "explanation_seed" not in ccols:
            conn.execute("ALTER TABLE concepts ADD COLUMN explanation_seed TEXT NOT NULL DEFAULT ''")
        if "explanation_user" not in ccols:
            conn.execute("ALTER TABLE concepts ADD COLUMN explanation_user TEXT")
        # 历史数据：当前 explanation 视为基线种子值，用户层初始为空（无覆盖）
        conn.execute(
            "UPDATE concepts SET explanation_seed = COALESCE(explanation, ''), explanation_user = NULL")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (27, ?)", (now(),))
        LOG.info("数据库已迁移到 v27 (概念详解分层 explanation_seed/explanation_user)")

    # v28: 学习台教材注册表 — 登记工作区内 md/txt/html/pdf 教材文件（路径相对 APP_DIR）。
    #      只存注册元数据，不动文件本体；path 唯一防重复登记；chapter_tree 预留目录缓存。
    if current < 28:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS materials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL DEFAULT 'physics',
                title TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                fmt TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'upload',
                chapter_tree TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_materials_subject ON materials(subject, updated_at)")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (28, ?)", (now(),))
        LOG.info("数据库已迁移到 v28 (学习台教材注册表 materials)")

    # v29: 学习台批注 — 高亮/旁注（P0.5 划词四连）。锚点存 JSON
    #      {prefix, quote, suffix} 三段模糊锚：渲染端在文本节点中定位 quote，
    #      失配时用 prefix/suffix 辅助模糊重定位；教材编辑致漂移由前端标记失效。
    if current < 29:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS annotations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                material_id INTEGER NOT NULL REFERENCES materials(id),
                kind TEXT NOT NULL,
                anchor TEXT NOT NULL,
                body TEXT,
                color TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_annotations_material ON annotations(material_id)")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (29, ?)", (now(),))
        LOG.info("数据库已迁移到 v29 (学习台批注 annotations)")

    # v30: M8 可选向量检索 — chunk 向量（embedding_model 未配置则不生成，纯增量增强）
    if current < 30:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rag_embeddings (
                chunk_id INTEGER PRIMARY KEY,
                dim INTEGER NOT NULL,
                model TEXT NOT NULL,
                vec BLOB NOT NULL,
                FOREIGN KEY(chunk_id) REFERENCES rag_chunks(id) ON DELETE CASCADE
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rag_embeddings_model ON rag_embeddings(model)")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (30, ?)", (now(),))
        LOG.info("数据库已迁移到 v30 (M8 向量检索 rag_embeddings)")

    # v31: 清洗 problems.concept_ids 的历史脏值 + 补 concept_links/hints 索引
    # 背景：库内曾同时存在三种互不兼容格式——
    #   "[]"      ← v10 建列时的 DEFAULT '[]'（JSON 风格），INSERT 省略本列即落入
    #   ",1,,,7," ← 旧 bind_problem 的 ",".join(f",{cid},") 多出空段
    #   ",1,7,"   ← 正确格式
    # 读取端靠 split+isdigit 侥幸不崩，但任何精确等值匹配都不可靠。
    # 写入端已统一为 graph.concept_csv()，此处只做一次性数据清洗。
    if current < 31:
        _v31_normalize_concept_ids(conn)
        # 索引取舍全部经实测（computer 学科，concept_links 10774 行 / hints）：
        #
        #   前向 concept_a = ?       0.012 ms → 无需建：表已有
        #                            PRIMARY KEY (concept_a, concept_b, relation)，
        #                            SQLite 的 sqlite_autoindex 已覆盖该方向。
        #                            早期草稿曾加 idx_links_a，属冗余（浪费空间+写放大）。
        #   反向 concept_b = ?       0.215 ms → 0.003 ms（67×）  ← 建 idx_links_b
        #   relation = 'prereq'      1.581 ms → 不建：结果占表 33%~55%，选择性太差，
        #                            索引 seek + 逐行回表反而比顺序全扫更贵
        #                            （实测加索引后 1.238 → 2.006 ms，净退化）。
        #                            该场景改用 JOIN concepts 在 SQL 侧过滤学科
        #                            （实测 1.215 → 0.743 ms，且复用 idx_links_b）。
        #   hints by problem_id      0.011 ms → 0.003 ms（3.6×） ← 建 idx_hints_problem
        #                            关键不在绝对耗时，而在消除 handler_problems
        #                            相关子查询「每道错题一次全表扫」。
        for ddl in (
            "CREATE INDEX IF NOT EXISTS idx_links_b ON concept_links(concept_b, relation)",
            "CREATE INDEX IF NOT EXISTS idx_hints_problem ON hints(problem_id, level)",
        ):
            conn.execute(ddl)
        # 清掉 v31 早期草稿里误建的冗余/负收益索引（若在旧副本上曾创建）
        for stale in ("idx_links_rel", "idx_links_a"):
            conn.execute(f"DROP INDEX IF EXISTS {stale}")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (31, ?)", (now(),))
        LOG.info("数据库已迁移到 v31 (concept_ids 格式清洗 + 图谱/提示索引)")

    # v32: D1 复习日志前后快照 — card_reviews 从「结果记录」升级为「不可变事实」
    #      每行携带评分前完整记忆态快照（prev_ 八列，D2 撤销 = 原子恢复）+
    #      评分后 FSRS 三态（cur_state/stability/difficulty，与 cards 行一致性校验用；
    #      cur_due/cur_interval 复用既有 due_date/interval_days 列，不冗余建列）+
    #      F2 参数指纹（fsrs_params_version；default 参数为空串）+
    #      D2 撤销标记（undone：1=已作废，撤销只针对最近一行未作废日志）。
    #      全部 ADD COLUMN 带 DEFAULT：旧库升级零损、旧代码可跑（回滚=撤代码不撤库）；
    #      旧数据行的 prev_due 为空串 → 不可撤销（无快照语义，undo 显式拒绝）。
    if current < 32:
        # 列存在性检查保证幂等：v4 重放测试会带着 v32 结构库重跑全部迁移
        rcols = {r[1] for r in conn.execute("PRAGMA table_info(card_reviews)").fetchall()}
        for col, ddl in (
            ("prev_state", "ALTER TABLE card_reviews ADD COLUMN prev_state INTEGER NOT NULL DEFAULT 0"),
            ("prev_stability", "ALTER TABLE card_reviews ADD COLUMN prev_stability REAL NOT NULL DEFAULT 0.0"),
            ("prev_difficulty", "ALTER TABLE card_reviews ADD COLUMN prev_difficulty REAL NOT NULL DEFAULT 0.0"),
            ("prev_due", "ALTER TABLE card_reviews ADD COLUMN prev_due TEXT NOT NULL DEFAULT ''"),
            ("prev_interval", "ALTER TABLE card_reviews ADD COLUMN prev_interval INTEGER NOT NULL DEFAULT 1"),
            ("prev_repetition", "ALTER TABLE card_reviews ADD COLUMN prev_repetition INTEGER NOT NULL DEFAULT 0"),
            ("prev_ease", "ALTER TABLE card_reviews ADD COLUMN prev_ease REAL NOT NULL DEFAULT 2.5"),
            ("prev_last_review", "ALTER TABLE card_reviews ADD COLUMN prev_last_review TEXT NOT NULL DEFAULT ''"),
            ("cur_state", "ALTER TABLE card_reviews ADD COLUMN cur_state INTEGER NOT NULL DEFAULT 0"),
            ("cur_stability", "ALTER TABLE card_reviews ADD COLUMN cur_stability REAL NOT NULL DEFAULT 0.0"),
            ("cur_difficulty", "ALTER TABLE card_reviews ADD COLUMN cur_difficulty REAL NOT NULL DEFAULT 0.0"),
            ("fsrs_params_version", "ALTER TABLE card_reviews ADD COLUMN fsrs_params_version TEXT NOT NULL DEFAULT ''"),
            ("undone", "ALTER TABLE card_reviews ADD COLUMN undone INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in rcols:
                conn.execute(ddl)
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (32, ?)", (now(),))
        LOG.info("数据库已迁移到 v32 (复习日志前后快照 + 撤销标记)")

    # v33: D3 回收站 — 删除前快照入 trash（payload_json 按表存全行），
    #      保留期内（settings.trash_retention_days，默认 3 日）可原样恢复；
    #      0 = 永不自动清理。恢复只标记 restored_at，不删行（审计可查）。
    if current < 33:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trash (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                entity_id INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                trashed_at TEXT NOT NULL,
                restored_at TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trash_entity ON trash(kind, entity_id, id)")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (33, ?)", (now(),))
        LOG.info("数据库已迁移到 v33 (回收站 trash 表)")

    # v34: D4 掌握度事件溯源 — update_progress 重算前后值落 mastery_events，
    #      只记实际变化的行（谁改的/依据什么/改前改后），全量重算仍是唯一写路径。
    if current < 34:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mastery_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL,
                concept_id INTEGER NOT NULL,
                entry_point TEXT NOT NULL DEFAULT 'other',
                evidence TEXT NOT NULL DEFAULT '',
                revision INTEGER NOT NULL DEFAULT 1,
                prev_mastery REAL NOT NULL DEFAULT 0.0,
                cur_mastery REAL NOT NULL DEFAULT 0.0,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mastery_events_concept "
            "ON mastery_events(subject, concept_id, id)")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (34, ?)", (now(),))
        LOG.info("数据库已迁移到 v34 (掌握度事件溯源 mastery_events)")

    # v35: G1/G2 图谱溯源 — 边可解释、概念可观察：
    #   concept_links 增 strength(hard|soft)/reason(一句)/evidence_ref(file,page 锚点)；
    #   concepts 增 evidence(达标判据 JSON 数组)/assessment_prompt(口试模板，含 {{name}})。
    #   存量边落默认（soft/空理由），种子边在加载时标 evidence_ref='seed'；不回填。
    if current < 35:
        # 表存在性守卫：v10 之前的旧库 / 最小化构造库可能无 concepts/concept_links，
        # 此时跳过加列（v10 迁移建表后，新库天然含最新列，无需补救）。
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "concept_links" in tables:
            lcols = {r[1] for r in conn.execute("PRAGMA table_info(concept_links)").fetchall()}
            if "strength" not in lcols:
                conn.execute("ALTER TABLE concept_links ADD COLUMN strength TEXT NOT NULL DEFAULT 'soft'")
            if "reason" not in lcols:
                conn.execute("ALTER TABLE concept_links ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
            if "evidence_ref" not in lcols:
                conn.execute("ALTER TABLE concept_links ADD COLUMN evidence_ref TEXT NOT NULL DEFAULT ''")
        if "concepts" in tables:
            ccols = {r[1] for r in conn.execute("PRAGMA table_info(concepts)").fetchall()}
            if "evidence" not in ccols:
                conn.execute("ALTER TABLE concepts ADD COLUMN evidence TEXT NOT NULL DEFAULT ''")
            if "assessment_prompt" not in ccols:
                conn.execute("ALTER TABLE concepts ADD COLUMN assessment_prompt TEXT NOT NULL DEFAULT ''")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (35, ?)", (now(),))
        LOG.info("数据库已迁移到 v35 (图谱溯源 concept_links 溯源列 + concepts 判据/口试模板)")


# 内置三科的中文显示名（title）；user 自定义过的中文 title 不会被覆盖。
_BUILTIN_SUBJECT_TITLES = {
    "physics": "物理",
    "chemistry": "化学",
    "math": "数学",
}


def register_builtin_subjects() -> None:
    """启动注册：内置三科 + data/ 下种子文件学科（幂等）。

    内置三科的中文 title 在首次注册时写入；对老库里 title 仍等于英文 id 的记录，
    也会补成中文（仅当 title 还是英文 id 时才改，避免覆盖用户自定义的中文名）。
    """
    from config import BUNDLE_ROOT
    import json as _json
    with DB_LOCK, db() as conn:
        for sid in ("physics", "chemistry", "math"):
            conn.execute(
                "INSERT OR IGNORE INTO subjects(id, title, builtin, created_at) VALUES (?, ?, 1, ?)",
                (sid, _BUILTIN_SUBJECT_TITLES[sid], now()),
            )
            # 老库兼容：title 仍是英文 id 时补成中文（用户已自定义中文名则跳过）
            conn.execute(
                "UPDATE subjects SET title = ? WHERE id = ? AND title = ?",
                (_BUILTIN_SUBJECT_TITLES[sid], sid, sid),
            )
            # 登记内置三科的种子版本，避免 seed_status 误报 needs_update
            seed_name = {"physics": "seed_concepts.json",
                         "chemistry": "seed_concepts_chemistry.json",
                         "math": "seed_concepts_math.json"}.get(sid, f"seed_concepts_{sid}.json")
            seed_path = BUNDLE_ROOT / "data" / seed_name
            ver = 0
            if seed_path.is_file():
                try:
                    ver = int((_json.loads(seed_path.read_text(encoding="utf-8")) or {}).get("version", 0) or 0)
                except (OSError, ValueError, _json.JSONDecodeError):
                    ver = 0
            conn.execute(
                "INSERT INTO seed_versions(subject, seed_version, applied_at) VALUES (?, ?, ?) "
                "ON CONFLICT(subject) DO UPDATE SET seed_version=excluded.seed_version, applied_at=excluded.applied_at",
                (sid, ver, now()),
            )
    seed_dir = BUNDLE_ROOT / "data"
    if not seed_dir.is_dir():
        return
    import json as _json
    with DB_LOCK, db() as conn:
        for p in sorted(seed_dir.glob("seed_concepts_*.json")):
            sid = normalize_subject(p.stem[len("seed_concepts_"):])
            if not sid or sid in ("physics", "chemistry", "math"):
                continue
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,19}", sid) or not p.stat().st_size:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO subjects(id, title, builtin, created_at) VALUES (?, ?, 0, ?)",
                (sid, sid, now()),
            )
            # 登记种子版本，使 seed_status 能识别"已是最新"
            try:
                ver = int((_json.loads(p.read_text(encoding="utf-8")) or {}).get("version", 0) or 0)
            except (OSError, ValueError, _json.JSONDecodeError):
                ver = 0
            conn.execute(
                "INSERT INTO seed_versions(subject, seed_version, applied_at) VALUES (?, ?, ?) "
                "ON CONFLICT(subject) DO UPDATE SET seed_version=excluded.seed_version, applied_at=excluded.applied_at",
                (sid, ver, now()),
            )


def list_subjects() -> list[dict[str, Any]]:
    """注册学科列表：内置在前，其余按创建时间。"""
    with DB_LOCK, db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, title, builtin, created_at FROM subjects ORDER BY builtin DESC, created_at, id"
        ).fetchall()]


def normalize_subject(subject_id: str) -> str:
    """学科 id 归一：统一小写，避免 Title-case 与全小写双副本（历史坑：Music/Music 并存致空壳科）。"""
    return str(subject_id or "").strip().lower()


def subject_exists(subject_id: str) -> bool:
    with DB_LOCK, db() as conn:
        return conn.execute(
            "SELECT 1 FROM subjects WHERE id = ?", (normalize_subject(subject_id),)
        ).fetchone() is not None


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DB_LOCK, db() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.executemany(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            DEFAULT_SETTINGS.items(),
        )
    LOG.info("数据库已初始化: %s", DB_PATH)
    register_builtin_subjects()
    import trash as _trash
    _trash.startup_purge()  # D3：启动时清理过期回收站快照

    # 性能索引（幂等，已存在则跳过）
    with DB_LOCK, db() as conn:
        _IDX_SQLS = [
            "CREATE INDEX IF NOT EXISTS idx_reviews_pid_comp ON reviews(problem_id, completed, id DESC)",
            "CREATE INDEX IF NOT EXISTS idx_reviews_due ON reviews(due_date, completed)",
            "CREATE INDEX IF NOT EXISTS idx_problems_course ON problems(course)",
            "CREATE INDEX IF NOT EXISTS idx_problems_topic ON problems(topic)",
        ]
        for sql in _IDX_SQLS:
            conn.execute(sql)
    LOG.info("数据库索引已确认")


def rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    # 持 DB_LOCK（RLock，重入安全）：防止与 close_all_connections/close_thread_conn
    # 并发时 execute 命中已关闭连接；代价是读串行化，单用户场景可接受。
    with DB_LOCK:
        with db() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]


def row(query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    result = rows(query, params)
    return result[0] if result else None


def settings_dict(include_secret: bool = False) -> dict[str, str]:
    """非敏感设置。D1 起密钥一律不读 DB：仅返回 env/内存/keys.enc 层合并后的密钥。"""
    import os
    data = {item["key"]: item["value"] for item in rows("SELECT key, value FROM settings")}
    data.pop("api_key", None)  # R4：DB 中永不存放密钥明文
    env_key = os.environ.get("LEARNOS_API_KEY", "")
    if env_key:
        data["api_key"] = env_key
        data["key_source"] = "environment"
    else:
        data["api_key"] = ""
        data["key_source"] = "none"
    if not include_secret:
        key = data.get("api_key", "")
        data["api_key"] = "••••••••" if key else ""
        data["has_api_key"] = bool(key)
    return data
