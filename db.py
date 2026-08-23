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
_ALL_CONNS: set[sqlite3.Connection] = set()
_ALL_CONNS_LOCK = threading.Lock()
_EPOCH = 0


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# 生产级 PRAGMA（§1.3/§16.1）：在 journal_mode=WAL 之后统一追加。
# mmap_size 在某些沙箱/文件系统中不可用，单独 try 避免连接失败。
_PROD_PRAGMAS = [
    "PRAGMA busy_timeout = 5000",          # 写竞争时等待而非立即报错
    "PRAGMA synchronous = NORMAL",         # WAL 下安全且显著降低 fsync 开销
    "PRAGMA cache_size = -64000",          # 64MB 页缓存（负值为 KB）
    "PRAGMA temp_store = MEMORY",          # 临时表/索引常驻内存
    "PRAGMA wal_autocheckpoint = 1000",    # 每 1000 页自动 checkpoint
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
            _ALL_CONNS.add(conn)
    return conn


def close_thread_conn() -> None:
    """关闭当前线程的本地连接（文件级备份/还原前调用）。

    连接复用后打开的连接会：① 锁住库文件导致 Windows rename/copy 失败；
    ② 未 checkpoint 的 WAL 数据仍在 -wal 文件中，直接拷 .db 会漏数据。
    """
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
            _ALL_CONNS.discard(conn)
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
        conns = list(_ALL_CONNS)
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
    seed_dir = BUNDLE_ROOT / "data"
    if not seed_dir.is_dir():
        return
    with DB_LOCK, db() as conn:
        for p in sorted(seed_dir.glob("seed_concepts_*.json")):
            sid = p.stem[len("seed_concepts_"):]
            if not sid or sid in ("physics", "chemistry", "math"):
                continue
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,19}", sid) or not p.stat().st_size:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO subjects(id, title, builtin, created_at) VALUES (?, ?, 0, ?)",
                (sid, sid, now()),
            )


def list_subjects() -> list[dict[str, Any]]:
    """注册学科列表：内置在前，其余按创建时间。"""
    with DB_LOCK, db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, title, builtin, created_at FROM subjects ORDER BY builtin DESC, created_at, id"
        ).fetchall()]


def subject_exists(subject_id: str) -> bool:
    with DB_LOCK, db() as conn:
        return conn.execute(
            "SELECT 1 FROM subjects WHERE id = ?", (subject_id,)
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
