"""Build the reviewed, content-addressed evaluation contract from the live corpus.

This is a curation tool, not part of application startup.  The generated JSON is
the frozen contract consumed by the evaluation runners.  Runtime scoring never
uses file-name hints.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.database import connect, init_db
from tests.evalset import ALL_CASES


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = ROOT / "docs" / "eval" / "gold-evidence-spans.json"

# These questions depended on project documents that the current ingestion
# policy intentionally excludes, or on files no longer present in the corpus.
CORPUS_MISSING = {
    "A15", "A16",
    "X01", "X02", "X04", "X05", "X06", "X08", "X09",
    "M02", "M06", "M07",
}
PLAN_DEPENDENT = {
    "A15", "A16", "X01", "X02", "X04", "X05", "X06", "X08", "X09", "M07",
}

# Filename-free alternatives that were verified as the same source family.
# Keeping the hashes here prevents a differently named but correct report from
# being scored as a miss (the defect that invalidated the post-cleanup baseline).
OS_SOURCE_FAMILY = {
    "0e9b2e73c48e71fa2c157b921af128f7e3bb1a5bb67e10a63036664e004d2a29",
    "d0987d33ea5026a4581497d2afe71a6d97e31a5431cdb5cf8705961146855dc4",
    "d2047824de3827fdde1b8cd7fbfe8b92da9e6391f3351f841ddfce5bfca7628b",
    "c9a488b9c2758f1dc67c03c11d5a95dac721a8bfeda0a61497516723f806367c",
    "350f029f4a3e724a63f7a44aef628a491fe5d1c966c828e3e4cd786e367c7cdd",
    "8399a545b317f55e219d358f4f10e9d7d2c7d3cdb96e91d7c6691ecf9c69c409",
    "48b757c295f79798be5e516fee57b2e29a654ee2295480e61df93a6d4ccc656c",
    "5557dda813fcf122e7f71ff1a372b19e15804cf250c29cd12e56e5d099955f06",
}
FTP_SOURCE_FAMILY = {
    "2ba99caac52fff351c3a0c172515e30c79e20a2872ad427979424b7ac7a0e5fe",
    "a96ed61b4205dd8710056b8a5e76236cedffe6c7e19c307f0c10eb0e2929c18c",
}
COMPUTER_EXAM_FAMILY = {
    "757b63a0db66da2bf060a8231b39c584546aabad85fe0ca0ac66ef1b2740fa08",
    "1fe13abaac723d641f06b87906868b4d872a4c219d371cff3aa4c97b129a98a4",
}


@dataclass(frozen=True)
class RequirementSpec:
    requirement_id: str
    group_index: int
    terms: tuple[str, ...]


REQUIREMENT_OVERRIDES: dict[str, tuple[RequirementSpec, ...]] = {
    "A10": (
        RequirementSpec("font-recommendation", 0, ("思源宋体", "思源黑体")),
        RequirementSpec("colour-palette", 0, ("深黛青 40%", "深黛青")),
    ),
    "F31": (
        RequirementSpec(
            "module-count", 0,
            ("10 个模块", "10个模块", "10 个独立模块", "10个独立模块"),
        ),
    ),
    "X03": (
        RequirementSpec("os-resource-state", 0, ("Available", "Need")),
        RequirementSpec("ftp-thread-model", 1, ("thread", "线程")),
        RequirementSpec("ftp-process-model", 1, ("process", "进程")),
        RequirementSpec("ftp-select-model", 1, ("select", "selectors", "事件驱动")),
    ),
    "X07": (
        RequirementSpec("inkhole-positioning", 0, ("P2P 文件传输", "P2P文件传输")),
        RequirementSpec("inkhole-transport", 0, ("TCP 直连", "TCP直连")),
        RequirementSpec("ftp-positioning", 1, ("RFC 959", "FTP 协议", "FTP协议")),
    ),
    "X10": (
        RequirementSpec("computer-register-file", 0, ("寄存器堆",)),
        RequirementSpec("computer-data-path", 0, ("数据通路",)),
        RequirementSpec("os-resource-vectors", 1, ("Available", "Allocation")),
        RequirementSpec("os-fat-structures", 1, ("FAT12", "FAT 表", "FAT表")),
    ),
}


def _requirements(case) -> tuple[RequirementSpec, ...]:
    if case.qid in REQUIREMENT_OVERRIDES:
        return REQUIREMENT_OVERRIDES[case.qid]
    return tuple(
        RequirementSpec(f"evidence-{index:02d}", 0, (keyword,))
        for index, keyword in enumerate(case.answer_keywords, start=1)
    )


def _document_texts(conn) -> tuple[dict[str, str], dict[str, list[str]]]:
    texts = {
        row["sha256"]: row["full_text"]
        for row in conn.execute(
            """SELECT c.sha256, d.full_text
               FROM contents c JOIN document_representations d ON d.content_id = c.id
               WHERE d.index_version = c.active_index_version"""
        )
    }
    names: dict[str, list[str]] = {}
    for row in conn.execute(
        """SELECT c.sha256, f.name FROM contents c
           JOIN files f ON f.content_id = c.id ORDER BY f.name"""
    ):
        names.setdefault(row["sha256"], []).append(row["name"])
    return texts, names


def _seed_hashes(hint: str, names: dict[str, list[str]], *, qid: str) -> set[str]:
    if hint == "PLAN":
        return set()
    if hint == "操作系统实验报告":
        return set(OS_SOURCE_FAMILY)
    if hint == "FTP服务器课程设计报告":
        return set(FTP_SOURCE_FAMILY)
    if hint == "计算机组成原理期末考试":
        return set(COMPUTER_EXAM_FAMILY)
    if qid == "M05":
        hint = "2026年春季学期学生体育课成绩评定具体要求"
    folded = hint.casefold()
    return {
        digest for digest, file_names in names.items()
        if any(folded in name.casefold() for name in file_names)
    }


def _group_hashes(
    case, group_index: int, hint: str, specs: tuple[RequirementSpec, ...],
    texts: dict[str, str], names: dict[str, list[str]],
) -> list[str]:
    candidates = _seed_hashes(hint, names, qid=case.qid)
    assigned = [spec for spec in specs if spec.group_index == group_index]
    if assigned:
        candidates = {
            digest for digest in candidates
            if digest in texts and all(
                any(term in texts[digest] for term in spec.terms)
                for spec in assigned
            )
        }
    return sorted(candidates)


def _quote_range(text: str, match_start: int, match_end: int) -> tuple[int, int]:
    left_candidates = [text.rfind(mark, 0, match_start) for mark in "。！？!?\n"]
    left = max(left_candidates, default=-1) + 1
    right_candidates = [
        pos for mark in "。！？!?\n"
        if (pos := text.find(mark, match_end)) >= 0
    ]
    right = min(right_candidates) + 1 if right_candidates else len(text)
    if right - left > 480:
        left = max(left, match_start - 200)
        right = min(right, match_end + 280)
    while left < match_start and text[left].isspace():
        left += 1
    while right > match_end and text[right - 1].isspace():
        right -= 1
    return left, right


def _iter_term_matches(text: str, term: str):
    """Yield exact term matches without accepting partial numeric tokens."""
    cursor = 0
    while True:
        match_start = text.find(term, cursor)
        if match_start < 0:
            return
        match_end = match_start + len(term)
        cursor = match_end
        left_is_numeric = (
            term[0].isdigit()
            and match_start > 0
            and (text[match_start - 1].isdigit() or text[match_start - 1] == ".")
        )
        right_is_numeric = (
            term[-1].isdigit()
            and match_end < len(text)
            and (text[match_end].isdigit() or text[match_end] == ".")
        )
        if not left_is_numeric and not right_is_numeric:
            yield match_start, match_end


def _requirement_alternatives(
    conn, source_hashes: list[str], spec: RequirementSpec,
    context_terms: tuple[str, ...],
) -> list[dict]:
    if not source_hashes:
        return []
    marks = ",".join("?" * len(source_hashes))
    rows = conn.execute(
        f"""SELECT c.sha256, ch.text_hash, ch.text, ch.start_offset, ch.end_offset
            FROM chunks ch JOIN contents c ON c.id = ch.content_id
            WHERE c.sha256 IN ({marks})
              AND ch.index_version = c.active_index_version
            ORDER BY c.sha256, ch.ordinal""",
        source_hashes,
    ).fetchall()
    candidates: list[tuple[int, int, int, dict]] = []
    seen: set[tuple[str, str, int, int]] = set()
    for row in rows:
        chunk_text = row["text"]
        for term in spec.terms:
            for match_start, match_end in _iter_term_matches(chunk_text, term):
                quote_start, quote_end = _quote_range(chunk_text, match_start, match_end)
                key = (row["sha256"], row["text_hash"], quote_start, quote_end)
                if key not in seen:
                    document_start = (
                        row["start_offset"] + quote_start
                        if row["start_offset"] is not None else None
                    )
                    document_end = (
                        row["start_offset"] + quote_end
                        if row["start_offset"] is not None else None
                    )
                    alternative = {
                        "content_sha256": row["sha256"],
                        "chunk_text_hash": row["text_hash"],
                        "quote": chunk_text[quote_start:quote_end],
                        "chunk_start_offset": quote_start,
                        "chunk_end_offset": quote_end,
                        "document_start_offset": document_start,
                        "document_end_offset": document_end,
                    }
                    quote = alternative["quote"]
                    coverage = sum(1 for candidate in context_terms if candidate in quote)
                    candidates.append((coverage, len(term), -len(quote), alternative))
                    seen.add(key)
    # A fact can be mentioned many times in a long report. Keep the three most
    # context-rich exact spans per content version; this preserves legitimate
    # duplicate-file alternatives without turning the frozen contract into a
    # dump of every incidental word occurrence.
    selected: list[dict] = []
    by_digest: dict[str, list[tuple[int, int, int, dict]]] = {}
    for candidate in candidates:
        by_digest.setdefault(candidate[3]["content_sha256"], []).append(candidate)
    for digest in sorted(by_digest):
        ranked = sorted(by_digest[digest], key=lambda item: item[:3], reverse=True)
        selected.extend(item[3] for item in ranked[:3])
    return selected


def build(conn) -> dict:
    texts, names = _document_texts(conn)
    cases = []
    scene_planning_sha = {
        digest for digest, values in names.items()
        if any(name.casefold() == "scene-planning.md" for name in values)
    }

    for case in ALL_CASES:
        if not case.answerable:
            cases.append({
                "qid": case.qid,
                "status": "unanswerable",
                "reason": case.note or "No supporting fact exists in the reviewed corpus.",
                "content_groups": [],
                "evidence_requirements": [],
            })
            continue
        if case.qid in CORPUS_MISSING:
            reason = (
                "The required Inktable PLAN source is excluded by the current "
                "source-code/config ingestion policy."
                if case.qid in PLAN_DEPENDENT else
                "The originally annotated document is not present in the current corpus."
            )
            cases.append({
                "qid": case.qid,
                "status": "corpus_missing",
                "reason": reason,
                "content_groups": [],
                "evidence_requirements": [],
            })
            continue

        specs = _requirements(case)
        status = "metadata" if case.category == "metadata" else "answerable"
        hints = case.doc_hints
        groups = []
        hashes_by_group: list[list[str]] = []
        for index, hint in enumerate(hints):
            hashes = _group_hashes(case, index, hint, specs, texts, names)
            if not hashes:
                raise RuntimeError(f"no gold content found for {case.qid}/{hint}")
            group_id = f"source-{index + 1}"
            groups.append({
                "group_id": group_id,
                "label": hint,
                "content_sha256": hashes,
            })
            hashes_by_group.append(hashes)

        requirements = []
        for spec in specs:
            if spec.group_index >= len(hashes_by_group):
                raise RuntimeError(
                    f"requirement {case.qid}/{spec.requirement_id} has invalid group index"
                )
            group_terms = tuple(dict.fromkeys(
                term
                for candidate in specs
                if candidate.group_index == spec.group_index
                for term in candidate.terms
            ))
            alternatives = _requirement_alternatives(
                conn, hashes_by_group[spec.group_index], spec, group_terms,
            )
            if not alternatives:
                raise RuntimeError(
                    f"no evidence span found for {case.qid}/{spec.requirement_id}: "
                    f"{spec.terms}"
                )
            requirements.append({
                "requirement_id": spec.requirement_id,
                "source_group": f"source-{spec.group_index + 1}",
                "alternatives": alternatives,
            })

        selected_hashes = {
            digest for group in groups for digest in group["content_sha256"]
        }
        if selected_hashes & scene_planning_sha:
            raise RuntimeError(
                f"{case.qid} selected scene-planning.md as Inktable PLAN gold"
            )
        cases.append({
            "qid": case.qid,
            "status": status,
            "reason": "",
            "content_groups": groups,
            "evidence_requirements": requirements,
        })

    return {
        "schema_version": 1,
        "contract": (
            "Content SHA-256 identifies valid source alternatives. Each evidence "
            "requirement is satisfied by overlap with any exact quoted span."
        ),
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.write and args.check:
        parser.error("--write and --check are mutually exclusive")

    conn = connect()
    init_db(conn)
    try:
        payload = build(conn)
    finally:
        conn.close()
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    counts: dict[str, int] = {}
    span_count = 0
    for case in payload["cases"]:
        counts[case["status"]] = counts.get(case["status"], 0) + 1
        span_count += sum(
            len(requirement["alternatives"])
            for requirement in case["evidence_requirements"]
        )

    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != rendered:
            print(f"gold contract is stale: {args.output}")
            return 1
    elif args.write:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(rendered)
    print(f"cases={counts} exact_span_alternatives={span_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
