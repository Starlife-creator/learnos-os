"""A2 概念知识图谱：种子加载、掌握度传播、题-概念绑定、先修告警。

- 层级：单元(0) → 章(1) → 概念(2)，概念拥有 prerequisite/related/contrast 三种关系。
- 掌握度：problems 绑定概念的平均 mastery 为自身掌握度；沿 prerequisite 边做
  最多两轮「先修门」传播（先修不熟 → 子概念打折），并存概念进度表。
- 种子数据 data/seed_concepts_<id>.json 仅在库空时一次性加载；用户增删改标记
  user_edited=1，不回写 seed 文件（改动仅存库）。

内容来源约定（concepts.source 列记录：seed/import/rag/ai/unknown）：
- **标准学科内容必须走种子文件**（data/seed_concepts_<id>.json + data/seed_questions_<id>.json），
  随 git 版本化、跨设备持久、可被 PR 审阅。机械/电子/英语/天文等新建学科均沿用此路径，
  **禁止直接写库**（库内 learnos.db 被 gitignore，换机即丢，且 ensure_seed 对非空学科永不重读）。
- 用户运行期导入/向导提取的内容落库（source=import/rag），可用 graph.export_seed() 反哺种子文件，
  形成「库 → 种子」内容闭环。
- 种子升级：seed_versions 表记录每学科已加载的种子版本；种子文件 version 提升后，ensure_seed
  会 LOG 提示「有新版标准图谱，可导出/合并」，但不自动覆盖以免丢失用户编辑。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from config import LOG, BUNDLE_ROOT
from db import db, now, row, rows, DB_LOCK

SEED_PATH = BUNDLE_ROOT / "data" / "seed_concepts.json"
PREREQ_THRESHOLD = 0.6
_LEVEL_UNIT = 0
_LEVEL_CHAPTER = 1
_LEVEL_CONCEPT = 2

# 学科：id -> 种子文件（physics 沿用旧文件名保持兼容；custom 学科无内置种子）
SUBJECT_SEEDS: dict[str, str] = {
    "physics": "seed_concepts.json",
    "chemistry": "seed_concepts_chemistry.json",
    "math": "seed_concepts_math.json",
}


def subject_seed_path(subject: str) -> Path:
    fname = SUBJECT_SEEDS.get(subject, f"seed_concepts_{subject}.json")
    return BUNDLE_ROOT / "data" / fname


def _load_seed(subject: str = "physics") -> dict[str, Any] | None:
    path = subject_seed_path(subject)
    if not path.is_file():
        return None  # 无种子文件的学科（网页端自建）为正常情况
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        LOG.warning("种子图谱文件不可读 (%s): %s", subject, exc)
        return None
    except json.JSONDecodeError as exc:
        LOG.warning("种子图谱文件解析失败 (%s): %s", subject, exc)
        return None


def ensure_seed(subject: str = "physics") -> None:
    """按学科从种子文件加载（幂等）。

    - 该学科 concepts 为空：首次加载，所有节点标记 source='seed'，并记录 seed_versions。
    - 该学科已有概念：比对种子文件 version 与 seed_versions 记录；
      若种子更新则 LOG 提示「有新版标准图谱，可导出/合并」（不强制覆盖，保留用户编辑）。
    """
    from db import normalize_subject
    subject = normalize_subject(subject)
    with DB_LOCK, db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM concepts WHERE subject = ?", (subject,)
        ).fetchone()["c"]
        seed = _load_seed(subject)
        seed_ver = int((seed or {}).get("version", 0) or 0)
        if count:
            # 已加载过：记录/比对种子版本，提示升级而非覆盖
            rec = conn.execute(
                "SELECT seed_version FROM seed_versions WHERE subject = ?", (subject,)
            ).fetchone()
            recorded = int(rec["seed_version"]) if rec else 0
            if seed and seed_ver > recorded:
                LOG.info(
                    "学科 %s 有新版标准图谱（种子 v%d > 已加载 v%d），可在设置页导出/合并；"
                    "当前不自动覆盖以免丢失用户编辑。",
                    subject, seed_ver, recorded,
                )
                conn.execute(
                    "INSERT INTO seed_versions(subject, seed_version, applied_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(subject) DO UPDATE SET seed_version=excluded.seed_version, applied_at=excluded.applied_at",
                    (subject, seed_ver, now()),
                )
            return
        if not seed:
            return
        with conn:
            name_to_id: dict[str, int] = {}
            # 第一遍：插入全部节点（含单元/章节），建立名字索引
            for unit in seed.get("units", []):
                unit_id = _insert_concept(conn, unit["name"], 0, 0, 0.1, name_to_id, subject, "seed")
                for chapter in unit.get("chapters", []):
                    ch_id = _insert_concept(conn, chapter["name"], unit_id, unit_id, 0.2, name_to_id, subject, "seed")
                    for c in chapter.get("concepts", []):
                        cid = _insert_concept(conn, c["n"], ch_id, ch_id, float(c.get("d", 0.5)),
                                              name_to_id, subject, "seed", str(c.get("desc", "") or ""))
                        name_to_id.setdefault(c["n"], cid)
            # 第二遍：建先修边（不依赖出现顺序）
            for unit in seed.get("units", []):
                for chapter in unit.get("chapters", []):
                    for c in chapter.get("concepts", []):
                        cid = name_to_id.get(c["n"])
                        if not cid:
                            continue
                        for prereq_name in c.get("p", []):
                            if prereq_name in name_to_id:
                                _insert_link(conn, name_to_id[prereq_name], cid, "prerequisite")
                            else:
                                LOG.warning("先修引用不存在，已跳过: %s", prereq_name)
            for relation, pairs in seed.get("links", {}).items():
                for a, b in pairs:
                    if a in name_to_id and b in name_to_id:
                        _insert_link(conn, name_to_id[a], name_to_id[b], relation)
                    else:
                        LOG.warning("%s 关系引用不存在，已跳过: %s-%s", relation, a, b)
            # 记录种子版本（首次加载）
            conn.execute(
                "INSERT INTO seed_versions(subject, seed_version, applied_at) VALUES (?, ?, ?) "
                "ON CONFLICT(subject) DO UPDATE SET seed_version=excluded.seed_version, applied_at=excluded.applied_at",
                (subject, seed_ver, now()),
            )
        LOG.info("种子图谱已加载 (%s): %d 概念 (v%d)", subject, len(name_to_id), seed_ver)


def seed_status(subject: str = "physics") -> dict[str, Any]:
    """返回学科种子同步状态：当前库概念数、已记录种子版本、种子文件版本、是否需要升级。"""
    seed = _load_seed(subject)
    seed_ver = int((seed or {}).get("version", 0) or 0)
    with DB_LOCK, db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM concepts WHERE subject = ?", (subject,)
        ).fetchone()["c"]
        rec = conn.execute(
            "SELECT seed_version FROM seed_versions WHERE subject = ?", (subject,)
        ).fetchone()
    recorded = int(rec["seed_version"]) if rec else 0
    return {
        "subject": subject,
        "concept_count": count,
        "seed_file_version": seed_ver,
        "loaded_version": recorded,
        "has_seed_file": seed is not None,
        "needs_update": bool(seed) and count > 0 and seed_ver > recorded,
    }


def _insert_concept(conn: Any, name: str, parent_id: int, chapter_id: int,
                    difficulty: float, name_to_id: dict[str, int], subject: str = "physics",
                    source: str = "unknown", explanation: str = "") -> int:
    cur = conn.execute(
        "SELECT id FROM concepts WHERE subject = ? AND name = ?", (subject, name)
    ).fetchone()
    if cur:
        return int(cur["id"])
    cursor = conn.execute(
        "INSERT INTO concepts(name, parent_id, chapter_id, difficulty, subject, created_at, "
        "source, explanation_seed, explanation_user, explanation) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        (name, parent_id, chapter_id, difficulty, subject, now(), source, explanation, explanation),
    )
    cid = int(cursor.lastrowid)
    name_to_id[name] = cid
    return cid


def _insert_link(conn: Any, a: int, b: int, relation: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO concept_links(concept_a, concept_b, relation) VALUES (?, ?, ?)",
        (a, b, relation),
    )


def load_graph(subject: str = "physics") -> dict[str, Any]:
    """返回指定学科图谱（节点含层级/难度/掌握度，边含关系）。

    level 纯内存推导：复用 nodes 列表的 parent_of 映射，零额外查询。
    旧实现逐节点 row() 每次新开连接（约 50ms/次），2650 概念 ≈ 135s；
    现改为单批 rows() + 内存映射，彻底消除 N 次连接。
    """
    from db import normalize_subject
    subject = normalize_subject(subject)
    ensure_seed(subject)
    nodes = rows("SELECT * FROM concepts WHERE subject = ? ORDER BY id", (subject,))
    parent_of = {n["id"]: n["parent_id"] for n in nodes}
    for n in nodes:
        n["level"] = _level_from_parents(n["parent_id"], parent_of)
    links = rows(
        "SELECT concept_a, concept_b, relation FROM concept_links "
        "WHERE concept_a IN (SELECT id FROM concepts WHERE subject = ?) "
        "ORDER BY CASE relation WHEN 'prerequisite' THEN 0 WHEN 'contrast' THEN 1 ELSE 2 END",
        (subject,),
    )
    return {"nodes": nodes, "links": links, "subject": subject}


def export_seed(subject: str, target_path: Path | None = None) -> Path:
    """把库内某学科的概念图谱导出为 seed_concepts_<subject>.json 形状（内容闭环）。

    重建单元/章节/概念的层级结构（按 parent_id/chapter_id），并带出先修边。
    兼容两种库内结构：标准 3 层（单元→章→概念，概念为 level 2）与扁平 2 层
    （单元→概念，概念 level 1、chapter_id 直接指向单元）。后者会为每个单元合成一个
    「概念」章节收纳其直接子概念，保证种子文件可被子话题加载器正确解析。
    user_edited=1 的概念也会被导出（用户编辑应可反哺标准种子）。
    返回写出的文件路径。
    """
    g = load_graph(subject)
    nodes = {n["id"]: n for n in g["nodes"]}
    links = g["links"]
    id_to_name = {n["id"]: n["name"] for n in g["nodes"]}
    # 先修边：concept_b 的先修 = concept_a
    prereq_of: dict[int, list[int]] = {}
    for lk in links:
        if lk["relation"] == "prerequisite":
            prereq_of.setdefault(lk["concept_b"], []).append(lk["concept_a"])

    units: dict[str, dict] = {}        # unit_name -> {name, chapters:[{name, concepts:[]}]}
    unit_of: dict[str, str] = {}       # chapter_name -> unit_name

    def get_unit(unit_name: str) -> dict:
        if unit_name not in units:
            units[unit_name] = {"name": unit_name, "chapters": []}
        return units[unit_name]

    def get_chapter(unit_name: str, ch_name: str) -> dict:
        u = get_unit(unit_name)
        for c in u["chapters"]:
            if c["name"] == ch_name:
                return c
        c = {"name": ch_name, "concepts": []}
        u["chapters"].append(c)
        return c

    for n in g["nodes"]:
        if n["level"] == _LEVEL_UNIT:
            get_unit(n["name"])
            continue
        ch = nodes.get(n["chapter_id"])
        # 概念归属：chapter_id 指向的节点若是单元(level0)，说明是 2 层结构，
        # 合成「概念」章节收纳；否则归入真实章节。
        if ch and ch["level"] == _LEVEL_UNIT:
            unit_name = ch["name"]
            ch_name = f"{unit_name}·概念"
        elif ch:
            unit_name = nodes.get(ch["parent_id"], {}).get("name") or ch["name"]
            ch_name = ch["name"]
        else:
            unit_name = nodes.get(n["parent_id"], {}).get("name") or "未分类"
            ch_name = "概念"
        concept_obj: dict[str, Any] = {"n": n["name"], "d": n["difficulty"]}
        exp = (n.get("explanation") or "").strip()
        if exp:
            concept_obj["desc"] = exp
        pres = prereq_of.get(n["id"], [])
        if pres:
            concept_obj["p"] = [id_to_name[p] for p in pres if p in id_to_name]
        get_chapter(unit_name, ch_name)["concepts"].append(concept_obj)

    out = {"version": 1, "subject": subject, "units": list(units.values())}
    path = target_path or subject_seed_path(subject)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _level_from_parents(parent_id: int, parent_of: dict[int, int]) -> int:
    """基于本学科 id→parent_id 映射做层级推导（与 _level_of 语义一致，零查询）。"""
    if parent_id == 0:
        return _LEVEL_UNIT
    grandparent = parent_of.get(parent_id)
    if grandparent is None:
        return _LEVEL_CONCEPT
    return _LEVEL_CHAPTER if grandparent == 0 else _LEVEL_CONCEPT


def _level_of(node: dict[str, Any]) -> int:
    if node["parent_id"] == 0:
        return _LEVEL_UNIT
    parent = row("SELECT parent_id FROM concepts WHERE id = ?", (node["parent_id"],))
    if not parent:
        return _LEVEL_CONCEPT
    return _LEVEL_CHAPTER if parent["parent_id"] == 0 else _LEVEL_CONCEPT


def _linked_ids(concept_id: int, relation: str, reverse: bool = False) -> list[int]:
    col_a, col_b = ("concept_b", "concept_a") if reverse else ("concept_a", "concept_b")
    return [r[col_b] for r in rows(
        f"SELECT {col_b} FROM concept_links WHERE {col_a} = ? AND relation = ?", (concept_id, relation),
    )]


def _self_mastery(conn: Any, concept_id: int, card_signal: tuple[float, int] | None = None) -> tuple[float, int]:
    """该概念被绑定题目的平均掌握度（0-1）与绑定题数。

    card_signal=(卡驱动掌握度, 卡样本数)：Phase 2 双驱动——当该概念存在已复习的
    概念闪卡时，把卡片评分信号与题目掌握度按样本数融合，使掌握度同时反映主动回忆表现。
    """
    subj = conn.execute("SELECT subject FROM concepts WHERE id = ?", (concept_id,)).fetchone()
    subject = subj["subject"] if subj else "physics"
    r = conn.execute("""
        SELECT AVG(mastery) / 5.0 AS m, COUNT(*) AS c FROM problems
        WHERE subject = ? AND concept_ids LIKE ?
    """, (subject, f"%,{concept_id},%")).fetchone()
    m = float(r["m"] or 0.0)
    count = int(r["c"] or 0)
    if card_signal:
        val, cnt = card_signal
        if cnt > 0:
            denom = count + cnt
            m = (m * count + val * cnt) / denom if denom > 0 else val
    return m, count


def card_mastery_signal(conn: Any, subject: str) -> dict[int, tuple[float, int]]:
    """Phase 2：统计该学科每概念的「闪卡驱动掌握度」——(0-1 均值, 卡样本数)。

    对每张 active 卡取最近 3 次评分的均值 / 4 作为该卡掌握度；再对同一概念下
    这些卡取平均。仅纳入已有 ≥1 次评分的卡（新卡未回忆前不拉低掌握度）。
    """
    rows_ = conn.execute(
        "SELECT c.concept_id AS cid, c.id AS card_id, cr.rating AS rating "
        "FROM cards c JOIN card_reviews cr ON cr.card_id = c.id "
        "WHERE c.subject = ? AND c.status = 'active' "
        "ORDER BY c.id DESC, cr.id DESC", (subject,)).fetchall()
    per_card: dict[int, list[int]] = {}
    for r_ in rows_:
        per_card.setdefault((r_["cid"], r_["card_id"]), []).append(int(r_["rating"]))
    acc: dict[int, list[float]] = {}
    for (cid, _card), ratings in per_card.items():
        val = sum(ratings[:3]) / len(ratings[:3]) / 4.0
        acc.setdefault(cid, []).append(val)
    out: dict[int, tuple[float, int]] = {}
    for cid, vals in acc.items():
        out[cid] = (round(sum(vals) / len(vals), 3), len(vals))
    return out


def concept_ids_to_list(raw: str) -> list[int]:
    """DB 内 concept_ids 为两侧逗号 CSV（如 ",1,7,"），解析为整数列表。"""
    try:
        return [int(x) for x in (raw or "").split(",") if x.strip().isdigit()]
    except (TypeError, ValueError):
        return []


_progress_ttl: dict[str, float] = {}


def _progress_key(subject: str) -> str:
    """TTL 键绑定 DB 路径：测试重建临时库后自动失效，避免跨库误命中。"""
    from db import DB_PATH
    return f"{DB_PATH}:{subject}"


def invalidate_progress_cache(subject: str | None = None) -> None:
    """主动失效掌握度 TTL 缓存（测试在重建 DB 后调用；生产路径也可按需调用）。"""
    if subject is None:
        _progress_ttl.clear()
    else:
        key = _progress_key(subject)
        _progress_ttl.pop(key, None)


def update_progress_cached(subject: str = "physics") -> None:
    """图谱加载用：带 15 秒 TTL 的掌握度重算（键含 DB 路径，测试重建库自动失效）。

    数据变化点必须调用 update_progress（永远重算），避免掌握度滞后。
    """
    import time as _time
    key = _progress_key(subject)
    now_ts = _time.monotonic()
    if _progress_ttl.get(key, 0) > now_ts:
        return
    update_progress(subject)
    _progress_ttl[key] = now_ts + 15.0


def update_progress(subject: str = "physics", force: bool = False) -> None:
    """重算掌握度：自身聚合 + 先修门传播两轮（A→B 表示 A 是先修）。

    显式调用 = 数据已变，永远立即重算（force 参数保留兼容，行为不变）。
    """
    ensure_seed(subject)
    with DB_LOCK, db() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM concepts WHERE parent_id <> 0 AND subject = ?", (subject,)
        ).fetchall()]
        # Phase 2：本学科概念的闪卡驱动掌握度（双驱动融合）
        card_m = card_mastery_signal(conn, subject)
        self_m: dict[int, float] = {}
        for cid in ids:
            m, cnt = _self_mastery(conn, cid, card_m.get(cid))
            self_m[cid] = m
            conn.execute(
                "INSERT OR REPLACE INTO concept_progress(concept_id, mastery, reviews, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (cid, m, cnt, now()),
            )
        links = conn.execute(
            "SELECT concept_a, concept_b FROM concept_links WHERE relation = 'prerequisite'"
        ).fetchall()
        prereq: dict[int, list[int]] = {}
        for l in links:
            prereq.setdefault(l["concept_b"], []).append(l["concept_a"])
        eff = dict(self_m)
        for _ in range(2):
            changed = False
            for cid in ids:
                parents = prereq.get(cid, [])
                if not parents:
                    continue
                gate = min(1.0, 0.5 + 0.5 * sum(eff.get(p, 0.0) for p in parents) / len(parents))
                new_val = self_m[cid] * gate
                if abs(new_val - eff[cid]) > 1e-6:
                    eff[cid] = new_val
                    changed = True
            if not changed:
                break
        for cid, val in eff.items():
            conn.execute(
                "UPDATE concept_progress SET mastery = ? WHERE concept_id = ?", (round(val, 3), cid),
            )


# Phase 3：按先修链的主动学习路径。掌握度 < 该阈值视为「待学/待补」。
LEARN_THRESHOLD = 0.6
_PATH_LIMIT = 20


def learning_path(subject: str = "physics", threshold: float = 0.6) -> dict[str, Any]:
    """主动学习路径：现在该学 / 该补的概念推荐（区别于到期复习队列）。

    规则（先修链）：
      - 概念「可学（ready）」= 其全部直接先修概念掌握度 ≥ threshold；
      - 弱概念 = 掌握度 < threshold 的叶子概念；
      - 推荐顺序：可学的弱概念按掌握度升序 → 若无可学弱概念，则取最弱被卡概念，
        并进一步推荐它最弱的那个未达标先修（先补底盘）。
    只读，不落库；掌握度已由题目+闪卡双驱动（Phase 2）给出。
    """
    from db import normalize_subject
    subject = normalize_subject(subject)
    ensure_seed(subject)
    threshold = 0.6 if not (0.3 <= float(threshold) <= 0.9) else float(threshold)
    with DB_LOCK, db() as conn:
        nodes = {n["id"]: dict(n) for n in conn.execute(
            "SELECT id, name, parent_id, chapter_id, difficulty, mastery_est "
            "FROM concepts WHERE subject = ? AND parent_id <> 0", (subject,)).fetchall()}
        prog = {r["concept_id"]: float(r["mastery"] or 0.0) for r in conn.execute(
            "SELECT cp.concept_id, cp.mastery FROM concept_progress cp "
            "JOIN concepts c ON c.id = cp.concept_id WHERE c.subject = ?", (subject,)).fetchall()}

        def master(cid: int) -> float:
            if cid in prog:
                return prog[cid]
            return float(nodes.get(cid, {}).get("mastery_est") or 0.0)

        # 叶子概念（无子节点的概念，排除空章）
        parent_ids = {n["parent_id"] for n in nodes.values()}
        leaf = [cid for cid in nodes if nodes[cid]["parent_id"] != 0 and cid not in parent_ids]

        # 先修边（仅限本学科节点）
        prereq_of: dict[int, list[int]] = {}
        for l in conn.execute("SELECT concept_a, concept_b FROM concept_links "
                              "WHERE relation = 'prerequisite'").fetchall():
            a, b = int(l["concept_a"]), int(l["concept_b"])
            if b in nodes and a in nodes:
                prereq_of.setdefault(b, []).append(a)

        def chapter_name(cid: int) -> str:
            ch = nodes.get(int(nodes[cid]["chapter_id"] or 0))
            return ch["name"] if ch else ""

        ready_weak, blocked = [], []
        learned = 0
        for cid in leaf:
            missing = [p for p in prereq_of.get(cid, []) if master(p) < threshold]
            val = master(cid)
            if val >= threshold:
                learned += 1
                continue
            entry = {"concept_id": cid, "name": nodes[cid]["name"],
                     "chapter": chapter_name(cid), "mastery": round(val * 100),
                     "difficulty": float(nodes[cid]["difficulty"] or 0.5)}
            if missing:
                entry["missing"] = [nodes[p]["name"] for p in missing]
                blocked.append(entry)
            else:
                ready_weak.append(entry)
        ready_weak.sort(key=lambda e: e["mastery"])
        blocked.sort(key=lambda e: e["mastery"])

        next_item = None
        if ready_weak:
            next_item = {**ready_weak[0], "reason": "ready"}  # 先学最弱且未被先修卡住的
        elif blocked and blocked[0]["missing"]:
            # 被卡（最弱）→ 指出应先补的未达标先修
            weak_blocked = blocked[0]
            gap_entries = sorted(
                [(p, master(p)) for p in prereq_of.get(weak_blocked["concept_id"], [])
                 if master(p) < threshold],
                key=lambda kv: kv[1])
            if gap_entries:
                p = gap_entries[0][0]
                next_item = {"concept_id": p, "name": nodes[p]["name"],
                             "chapter": chapter_name(p), "mastery": round(master(p) * 100),
                             "difficulty": float(nodes[p].get("difficulty") or 0.5),
                             "reason": "prerequisite", "for": weak_blocked["name"]}
    return {
        "subject": subject,
        "threshold": threshold,
        "now": next_item,
        "ready_weak": ready_weak[:_PATH_LIMIT],
        "blocked": blocked[:_PATH_LIMIT],
        "stats": {
            "total": len(leaf),
            "weak_ready": len(ready_weak),
            "weak_blocked": len(blocked),
            "learned": learned,
        },
    }


def _concept_ids_of(problem: dict[str, Any]) -> list[int]:
    return concept_ids_to_list(problem.get("concept_ids") or "")


def bind_problem(problem_id: int) -> list[int]:
    """题-概念绑定：本地关键词匹配（图谱概念名命中 topic>title>content）。

    绑定结果属于结构化校验（概念必须存在于图谱），直接落库不违反 R3。
    """
    ensure_seed()
    problem = row("SELECT * FROM problems WHERE id = ?", (problem_id,))
    if not problem:
        return []
    concept_ids = _local_bind(problem)
    csv = ",".join(f",{cid}," for cid in concept_ids) or ""
    with DB_LOCK, db() as conn:
        conn.execute("UPDATE problems SET concept_ids = ? WHERE id = ?", (csv, problem_id))
    update_progress(force=True)
    return concept_ids


def _local_bind(problem: dict[str, Any]) -> list[int]:
    """本地降级：题目 topic > title > content 依次做概念名包含匹配，命中即绑定。"""
    ensure_seed()
    subject = problem.get("subject") or "physics"
    text_fields = [problem.get("topic", ""), problem.get("title", ""), problem.get("content", "")]
    hits: list[int] = []
    for text in text_fields:
        if not text:
            continue
        for c in rows("SELECT id, name FROM concepts WHERE parent_id <> 0 AND subject = ?", (subject,)):
            if c["name"] and c["name"] in text:
                hits.append(int(c["id"]))
        if hits:
            break
    # 去重保序
    seen: set[int] = set()
    return [x for x in hits if not (x in seen or seen.add(x))]


def prereq_warnings(problem_id: int) -> list[dict[str, Any]]:
    """先修检测：返回绑定概念中「先修概念掌握度低」的告警列表。"""
    ensure_seed()
    problem = row("SELECT * FROM problems WHERE id = ?", (problem_id,))
    if not problem:
        return []
    warnings: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for cid in _concept_ids_of(problem):
        for prereq_id in _linked_ids(cid, "prerequisite", reverse=True):
            if (cid, prereq_id) in seen:
                continue
            seen.add((cid, prereq_id))
            p = row("SELECT * FROM concepts WHERE id = ?", (prereq_id,))
            if not p:
                continue
            prog = row("SELECT mastery FROM concept_progress WHERE concept_id = ?", (prereq_id,))
            mastery = prog["mastery"] if prog else (p["mastery_est"] or 0.0)
            if mastery < PREREQ_THRESHOLD:
                warnings.append({
                    "concept_id": prereq_id,
                    "name": p["name"],
                    "mastery": round(mastery * 100),
                })
    return warnings


def prereq_chain(concept_id: int) -> list[int]:
    """先修链（含自身与全部递归先修），用于「先修模式」过滤历史错题。"""
    ensure_seed()
    chain: list[int] = []
    stack = [concept_id]
    while stack:
        cid = stack.pop()
        if cid in chain:
            continue
        chain.append(cid)
        for p in _linked_ids(cid, "prerequisite", reverse=True):
            stack.append(p)
    return chain


def problems_for_concepts(concept_ids: list[int], limit: int = 100) -> list[dict[str, Any]]:
    """绑定到任一给定概念的题目列表（先修模式相关错题）。"""
    if not concept_ids:
        return []
    sql = " OR ".join("concept_ids LIKE ?" for _ in concept_ids)
    params = tuple(f"%,{cid},%" for cid in concept_ids)
    with DB_LOCK, db() as conn:
        cur = conn.execute(
            f"SELECT id, title, topic, mastery, error_type FROM problems "
            f"WHERE {sql} ORDER BY id DESC LIMIT {limit}",
            params,
        )
        return [dict(r) for r in cur.fetchall()]


def add_concept(name: str, parent_id: int = 0, subject: str = "physics",
                source: str = "import") -> int | None:
    """用户/向导新增概念（user_edited=1，不回写 seed）。

    source 标记来源：import=用户手动/批量导入，rag=资料导入向导提取，ai=AI 生成。
    parent_id 为章节点 id。
    """
    name = str(name).strip()
    if not name:
        return None
    parent = None
    if parent_id:
        parent = row("SELECT * FROM concepts WHERE id = ?", (parent_id,))
        if parent and parent.get("subject") != subject:
            return None
    chapter_id = 0
    if parent:
        if _level_of(parent) == _LEVEL_CHAPTER:
            chapter_id = parent["id"]
        else:
            chapter_id = parent["chapter_id"]
    with DB_LOCK, db() as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO concepts(name, parent_id, chapter_id, difficulty, user_edited, created_at, subject, source) "
                "VALUES (?, ?, ?, 0.5, 1, ?, ?, ?)",
                (name, parent_id, chapter_id, now(), subject, source),
            )
        except Exception as exc:
            LOG.warning("新增概念失败（可能重名）: %s", exc)
            return None
        return int(cursor.lastrowid)


def update_aliases(concept_id: int, aliases: str) -> bool:
    """更新概念别名（逗号分隔，如 "N2L,F=ma"）。用于未链接提及的别名匹配。"""
    cleaned = ",".join(a.strip()[:40] for a in str(aliases or "").replace("，", ",").split(",") if a.strip())
    with DB_LOCK, db() as conn:
        cur = conn.execute(
            "UPDATE concepts SET aliases = ? WHERE id = ?", (cleaned, concept_id))
    invalidate_mentions()
    return cur.rowcount > 0


def update_explanation(concept_id: int, explanation: str) -> bool:
    """更新概念详解（用户覆盖层 explanation_user）。

    - 非空串：写入用户层，显示值 explanation 同时等于该覆盖值。
    - 空串：清空用户层（explanation_user=NULL），显示值回档到种子基线
      （explanation 落回 explanation_seed）。用于「回退到种子」语义。
    种子基线 explanation_seed 永不被本函数修改，故重跑 apply 不会冲掉用户编辑。
    """
    text = str(explanation or "").strip()
    with DB_LOCK, db() as conn:
        if text == "":
            cur = conn.execute(
                "UPDATE concepts SET explanation_user = NULL, explanation = explanation_seed "
                "WHERE id = ?", (concept_id,))
        else:
            cur = conn.execute(
                "UPDATE concepts SET explanation_user = ?, explanation = ? WHERE id = ?",
                (text, text, concept_id))
    return cur.rowcount > 0


_mentions_cache: dict[str, tuple[str, list[dict[str, Any]]]] = {}


def invalidate_mentions(subject: str | None = None) -> None:
    """显式失效提及缓存（指纹是秒级时间戳，同秒内的绑定/别名变更需主动失效）。"""
    if subject is None:
        _mentions_cache.clear()
    else:
        _mentions_cache.pop(subject, None)


def unlinked_mentions(subject: str = "physics") -> list[dict[str, Any]]:
    """Obsidian 式未链接提及：扫描错题文本中出现但未绑定的概念（含别名）。

    纯本地字符串匹配，不依赖 AI。返回建议列表（按概念出现次数排序）。
    带指纹缓存：概念/错题数量与最后更新时间不变时直接复用上次结果，
    避免大库（概念×错题全量扫）在每次切页时重复执行。
    """
    with DB_LOCK, db() as conn:
        fp_row = conn.execute(
            "SELECT (SELECT COUNT(*) FROM problems WHERE subject = ?) || ':' || "
            "(SELECT COALESCE(MAX(updated_at), '') FROM problems WHERE subject = ?) || ':' || "
            "(SELECT COUNT(*) FROM concepts WHERE subject = ?) || ':' || "
            "(SELECT COALESCE(MAX(created_at), '') FROM concepts WHERE subject = ?) AS fp",
            (subject, subject, subject, subject)).fetchone()
    fingerprint = str(fp_row["fp"]) if fp_row else ""
    cached = _mentions_cache.get(subject)
    if cached and cached[0] == fingerprint:
        return cached[1]
    result = _scan_unlinked(subject)
    _mentions_cache[subject] = (fingerprint, result)
    return result


def _scan_unlinked(subject: str) -> list[dict[str, Any]]:
    concepts = rows("SELECT id, name, aliases FROM concepts WHERE subject = ?", (subject,))
    problems = rows(
        "SELECT id, title, topic, content, concept_ids FROM problems WHERE subject = ?", (subject,))
    id_to_name = {c["id"]: c["name"] for c in concepts}
    # 概念名/别名 → id（按长度降序匹配，避免"力"误命中"力学"）
    names: list[tuple[str, int]] = []
    for c in concepts:
        names.append((c["name"], c["id"]))
        for a in (c["aliases"] or "").split(","):
            if a.strip():
                names.append((a.strip(), c["id"]))
    names.sort(key=lambda x: len(x[0]), reverse=True)
    suggestions: dict[tuple[int, int], dict[str, Any]] = {}
    for p in problems:
        bound = {int(x) for x in str(p["concept_ids"] or "").split(",") if x.strip().isdigit()}
        text = f"{p['title']}\n{p['topic']}\n{p['content'] or ''}"
        for name, cid in names:
            if len(name) < 2 or cid in bound:
                continue  # 单字概念误报率高，跳过
            if name in text:
                key = (p["id"], cid)
                if key not in suggestions:
                    suggestions[key] = {"problem_id": p["id"], "problem_title": p["title"][:60],
                                        "concept_id": cid, "concept_name": id_to_name[cid],
                                        "matched": name}
    out = list(suggestions.values())
    out.sort(key=lambda s: -s["problem_id"])
    return out[:50]


def bind_concept(problem_id: int, concept_id: int, subject: str = "physics") -> bool:
    """把概念绑定到错题（concept_ids 追加，规范 ,id, 格式）。"""
    problem = row("SELECT concept_ids FROM problems WHERE id = ?", (problem_id,))
    concept = row("SELECT id FROM concepts WHERE id = ? AND subject = ?", (concept_id, subject))
    if not problem or not concept:
        return False
    ids = [int(x) for x in str(problem["concept_ids"] or "").split(",") if x.strip().isdigit()]
    if concept_id in ids:
        return True
    ids.append(concept_id)
    csv = f",{','.join(str(i) for i in ids)},"
    with DB_LOCK, db() as conn:
        conn.execute("UPDATE problems SET concept_ids = ?, updated_at = ? WHERE id = ?",
                     (csv, now(), problem_id))
    update_progress(subject, force=True)
    invalidate_mentions(subject)
    return True


def delete_concept(concept_id: int) -> bool:
    """删除概念（级联删除其关系边；chapter/unit 级需先确认）。"""
    with DB_LOCK, db() as conn:
        node = conn.execute("SELECT * FROM concepts WHERE id = ?", (concept_id,)).fetchone()
        if not node:
            return False
        children = conn.execute("SELECT COUNT(*) AS c FROM concepts WHERE parent_id = ?", (concept_id,)).fetchone()["c"]
        if children:
            return False
        bound = conn.execute("SELECT COUNT(*) AS c FROM problems WHERE concept_ids LIKE ?", (f"%,{concept_id},%",)).fetchone()["c"]
        with conn:
            conn.execute("DELETE FROM concept_links WHERE concept_a = ? OR concept_b = ?", (concept_id, concept_id))
            conn.execute("DELETE FROM concept_progress WHERE concept_id = ?", (concept_id,))
            conn.execute("DELETE FROM concepts WHERE id = ?", (concept_id,))
        return True


# Phase 3：手动画线（概念↔概念建边）。relation 与 DB CHECK 白名单一致。
_LINK_RELATIONS = {"prerequisite", "related", "contrast", "analogy", "inclusion", "progression"}
# 有方向语义的关系（先修/演进）：a 是 b 的前提/由 a 演进到 b（保留方向）
_LINK_DIRECTED = {"prerequisite", "progression"}


def link_concepts(a: int, b: int, relation: str, subject: str = "physics") -> tuple[bool, str]:
    """在两个概念间建一条 relation 边（幂等）。

    - prerequisite 保留方向：a 是 b 的先修（供学习路径/先修门正确读取）；
    - related/contrast 对称：规整为 (min, max) 防反向重复；
    - 校验关系白名单、两端存在且同学科、禁自环；`INSERT OR IGNORE` 幂等；
    - 建先修边后重算掌握度传播，使学习路径/进度即时反映。
    返回 (成功?, 错误信息)。
    """
    relation = str(relation or "").strip()
    if relation not in _LINK_RELATIONS:
        return False, "不支持的关系类型（须为 prerequisite/related/contrast）"
    if a == b:
        return False, "不能与自身连线"
    with DB_LOCK, db() as conn:
        na = conn.execute("SELECT subject FROM concepts WHERE id = ?", (a,)).fetchone()
        nb = conn.execute("SELECT subject FROM concepts WHERE id = ?", (b,)).fetchone()
        if not na or not nb:
            return False, "概念不存在"
        if na["subject"] != nb["subject"]:
            return False, "跨学科的概念不能连线"
        if na["subject"] != subject:
            return False, "概念不属于当前学科"
        if relation in _LINK_DIRECTED:
            ca, cb = a, b
        else:
            ca, cb = (a, b) if a < b else (b, a)
        conn.execute(
            "INSERT OR IGNORE INTO concept_links(concept_a, concept_b, relation) VALUES (?, ?, ?)",
            (ca, cb, relation))
    if relation == "prerequisite":
        update_progress(subject, force=True)
    return True, ""
