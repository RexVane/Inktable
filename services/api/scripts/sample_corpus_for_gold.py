"""为扩充评测集抽样候选段落（只读）。

出题必须**只看文档原文**，不看任何检索结果 —— 否则会不自觉地照着现有实现
出题，指标失去意义（HANDOFF H12）。本工具按文件类型分层抽出正文段落，
供人工据此出题；它不调用检索管线，也不打印任何排序信息。

    .venv/Scripts/python.exe scripts/sample_corpus_for_gold.py --per-ext 4 --seed 7
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.database import DB_PATH  # noqa: E402
from app.db.visibility import VISIBLE_FILES_COND  # noqa: E402

# 已被现有 77 题占用的内容，避免重复出题
from tests.evalset import ALL_CASES  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path(DB_PATH))
    parser.add_argument("--per-ext", type=int, default=4)
    parser.add_argument("--min-chars", type=int, default=220)
    parser.add_argument("--max-chars", type=int, default=1400)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        f"""SELECT ch.id, ch.content_id, ch.text, ch.section_path, ch.ordinal,
                   c.sha256, f.name, lower(coalesce(f.ext, '')) AS ext
            FROM chunks ch
            JOIN contents c ON c.id = ch.content_id
                           AND ch.index_version = c.active_index_version
            JOIN files f ON f.content_id = ch.content_id
            LEFT JOIN sources s ON s.id = f.source_id
            WHERE {VISIBLE_FILES_COND}
              AND ch.layer = 'child'
              AND length(ch.text) BETWEEN ? AND ?
            """,
        (args.min_chars, args.max_chars),
    ).fetchall()
    conn.close()

    # 现有题目已经覆盖过的文件名片段，抽样时避开，避免重复出题
    used_hints = {
        hint.lower()
        for case in ALL_CASES for hint in case.doc_hints
    }

    by_ext: dict[str, list[sqlite3.Row]] = {}
    seen_content: set[int] = set()
    rng = random.Random(args.seed)
    rng.shuffle(rows)
    for row in rows:
        name = str(row["name"]).lower()
        if any(hint in name for hint in used_hints):
            continue
        if row["content_id"] in seen_content:
            continue
        bucket = by_ext.setdefault(row["ext"] or "(无)", [])
        if len(bucket) >= args.per_ext:
            continue
        seen_content.add(row["content_id"])
        bucket.append(row)

    payload = []
    for ext in sorted(by_ext):
        for row in by_ext[ext]:
            payload.append({
                "ext": ext,
                "file": row["name"],
                "sha256": row["sha256"],
                "chunk_id": row["id"],
                "section_path": row["section_path"] or "",
                "text": row["text"],
            })

    for item in payload:
        print("=" * 78)
        print(f"[{item['ext']}] {item['file']}   sha={item['sha256'][:12]}  "
              f"chunk={item['chunk_id']}")
        if item["section_path"]:
            print(f"  标题路径: {item['section_path']}")
        print(item["text"])
    print("=" * 78)
    print(f"共 {len(payload)} 段，覆盖扩展名 {sorted(by_ext)}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        print(f"明细写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
