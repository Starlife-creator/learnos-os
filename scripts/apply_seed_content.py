#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""种子内容应用工具：把 seed_explanations_<id>.json 的详解写入库 concepts.explanation。

用法:
  python scripts/apply_seed_content.py <subject> [--commit]
  python scripts/apply_seed_content.py all [--commit]

- 图谱(seed_concepts_<id>.json) 与题库(seed_questions_<id>.json) 由 graph.ensure_seed /
  bank.load_bank 在运行时自动读取，无需本脚本。
- 概念详解因库被 gitignore，需本脚本在首次建库/克隆后执行一次，使跨设备可重建完整内容。
- 默认 dry-run 校验覆盖完整性；--commit 才写库（单事务）。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from db import db  # noqa: E402

DATA = os.path.join(ROOT, "data")


def _load_explanations(subject: str) -> dict[str, str]:
    p = os.path.join(DATA, f"seed_explanations_{subject}.json")
    if not os.path.isfile(p):
        return {}
    try:
        data = json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def apply_subject(subject: str, commit: bool) -> int:
    expl = _load_explanations(subject)
    if not expl:
        print(f"[{subject}] 无 seed_explanations_{subject}.json，跳过")
        return 0
    with db() as conn:
        rows = conn.execute(
            "SELECT name FROM concepts WHERE subject=? ORDER BY id", (subject,)).fetchall()
    name_set = {r["name"] for r in rows}
    # 仅以「详解文件名」为基准校验：所有详解条目必须能在库中找到对应概念节点
    # （库里单元/章节等非叶子节点本就无详解，不计入 missing）。
    missing = [n for n in expl if n not in name_set]   # 详解有、库里无 -> 严重不一致
    extra = [n for n in name_set if n not in expl]      # 库有、详解无 -> 仅信息提示（多为单元/章）
    empty = [n for n in expl if not str(expl[n]).strip()]
    n_leaf = len(expl)
    print(f"[{subject}] 库节点={len(name_set)} 详解(叶子)={n_leaf} "
          f"missing={len(missing)} 库无详解节点={len(extra)} empty={len(empty)}")
    if missing:
        print("  MISSING(详解无对应库概念):", missing[:10], "..." if len(missing) > 10 else "")
    if empty:
        print("  EMPTY:", empty[:10], "..." if len(empty) > 10 else "")
    if missing or empty:
        print(f"[{subject}] 校验未通过（存在详解找不到库概念或空详解），不写库")
        return -1
    if not commit:
        print(f"[{subject}] 校验通过（dry-run，未写库）。加 --commit 执行写入。")
        return 0
    with db() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN")
        written = 0
        for name, text in expl.items():
            cur.execute(
                "UPDATE concepts SET explanation=? WHERE subject=? AND name=?",
                (text, subject, name))
            written += cur.rowcount
        conn.commit()
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM concepts WHERE subject=? AND explanation<>''",
            (subject,)).fetchone()["c"]
        print(f"[{subject}] 已写入 {written} 条详解；库内非空详解={cnt}")
    return 0


def main():
    if len(sys.argv) < 2:
        print("用法: python scripts/apply_seed_content.py <subject|all> [--commit]")
        sys.exit(2)
    arg = sys.argv[1]
    commit = "--commit" in sys.argv
    if arg == "all":
        # 扫描所有 seed_explanations_*.json
        import glob
        subs = []
        for p in sorted(glob.glob(os.path.join(DATA, "seed_explanations_*.json"))):
            s = os.path.basename(p)[len("seed_explanations_"):-len(".json")]
            subs.append(s)
        rc = 0
        for s in subs:
            if apply_subject(s, commit) < 0:
                rc = 1
        sys.exit(rc)
    else:
        sys.exit(0 if apply_subject(arg, commit) >= 0 else 1)


if __name__ == "__main__":
    main()
