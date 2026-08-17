"""Stable content-addressed gold annotations for retrieval evaluations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


GOLD_SCHEMA_VERSION = 1
DEFAULT_GOLD_PATH = (
    Path(__file__).resolve().parents[3] / "docs" / "eval" / "gold-evidence-spans.json"
)
VALID_STATUSES = {"answerable", "metadata", "corpus_missing", "unanswerable"}


@dataclass(frozen=True)
class GoldSpan:
    content_sha256: str
    chunk_text_hash: str
    quote: str
    chunk_start_offset: int
    chunk_end_offset: int
    document_start_offset: int | None
    document_end_offset: int | None


@dataclass(frozen=True)
class EvidenceRequirement:
    requirement_id: str
    source_group: str
    alternatives: tuple[GoldSpan, ...]


@dataclass(frozen=True)
class ContentGroup:
    group_id: str
    label: str
    content_sha256: frozenset[str]


@dataclass(frozen=True)
class GoldCase:
    qid: str
    status: str
    reason: str
    content_groups: tuple[ContentGroup, ...]
    evidence_requirements: tuple[EvidenceRequirement, ...]


def _required_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"gold field {field!r} must be a non-empty string")
    return value


def _optional_offset(value: object, field: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"gold field {field!r} must be a non-negative integer or null")
    return value


def _parse_span(raw: dict) -> GoldSpan:
    chunk_start = _optional_offset(raw.get("chunk_start_offset"), "chunk_start_offset")
    chunk_end = _optional_offset(raw.get("chunk_end_offset"), "chunk_end_offset")
    if chunk_start is None or chunk_end is None or chunk_end <= chunk_start:
        raise ValueError("gold chunk offsets must describe a non-empty range")
    document_start = _optional_offset(
        raw.get("document_start_offset"), "document_start_offset",
    )
    document_end = _optional_offset(
        raw.get("document_end_offset"), "document_end_offset",
    )
    if (document_start is None) != (document_end is None):
        raise ValueError("gold document offsets must either both be set or both be null")
    if document_start is not None and document_end <= document_start:
        raise ValueError("gold document offsets must describe a non-empty range")
    quote = _required_str(raw.get("quote"), "quote")
    if len(quote) != chunk_end - chunk_start:
        raise ValueError("gold quote length does not match chunk offsets")
    return GoldSpan(
        content_sha256=_required_str(raw.get("content_sha256"), "content_sha256"),
        chunk_text_hash=_required_str(raw.get("chunk_text_hash"), "chunk_text_hash"),
        quote=quote,
        chunk_start_offset=chunk_start,
        chunk_end_offset=chunk_end,
        document_start_offset=document_start,
        document_end_offset=document_end,
    )


def load_gold(path: str | Path | None = None) -> dict[str, GoldCase]:
    gold_path = Path(path) if path else DEFAULT_GOLD_PATH
    payload = json.loads(gold_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != GOLD_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported gold schema {payload.get('schema_version')!r}; "
            f"expected {GOLD_SCHEMA_VERSION}"
        )
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError("gold cases must be a list")

    cases: dict[str, GoldCase] = {}
    for raw in raw_cases:
        qid = _required_str(raw.get("qid"), "qid")
        if qid in cases:
            raise ValueError(f"duplicate gold qid: {qid}")
        status = _required_str(raw.get("status"), "status")
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid gold status for {qid}: {status}")

        groups: list[ContentGroup] = []
        group_ids: set[str] = set()
        for group_raw in raw.get("content_groups", []):
            group_id = _required_str(group_raw.get("group_id"), "group_id")
            if group_id in group_ids:
                raise ValueError(f"duplicate content group {group_id!r} for {qid}")
            hashes = group_raw.get("content_sha256")
            if not isinstance(hashes, list) or not all(
                isinstance(item, str) and len(item) == 64 for item in hashes
            ):
                raise ValueError(f"invalid content hashes for {qid}/{group_id}")
            if status in {"answerable", "metadata"} and not hashes:
                raise ValueError(f"empty content group for evaluable case {qid}/{group_id}")
            group_ids.add(group_id)
            groups.append(ContentGroup(
                group_id=group_id,
                label=_required_str(group_raw.get("label"), "label"),
                content_sha256=frozenset(hashes),
            ))

        requirements: list[EvidenceRequirement] = []
        requirement_ids: set[str] = set()
        for requirement_raw in raw.get("evidence_requirements", []):
            requirement_id = _required_str(
                requirement_raw.get("requirement_id"), "requirement_id",
            )
            source_group = _required_str(
                requirement_raw.get("source_group"), "source_group",
            )
            if requirement_id in requirement_ids:
                raise ValueError(f"duplicate requirement {qid}/{requirement_id}")
            if source_group not in group_ids:
                raise ValueError(
                    f"requirement {qid}/{requirement_id} references unknown group "
                    f"{source_group!r}"
                )
            alternatives_raw = requirement_raw.get("alternatives")
            if not isinstance(alternatives_raw, list) or not alternatives_raw:
                raise ValueError(f"requirement {qid}/{requirement_id} has no alternatives")
            alternatives = tuple(_parse_span(item) for item in alternatives_raw)
            group = next(item for item in groups if item.group_id == source_group)
            if any(span.content_sha256 not in group.content_sha256 for span in alternatives):
                raise ValueError(
                    f"requirement {qid}/{requirement_id} contains an out-of-group hash"
                )
            requirement_ids.add(requirement_id)
            requirements.append(EvidenceRequirement(
                requirement_id=requirement_id,
                source_group=source_group,
                alternatives=alternatives,
            ))

        if status == "answerable" and (not groups or not requirements):
            raise ValueError(f"answerable case {qid} needs content groups and evidence")
        if status == "metadata" and (not groups or requirements):
            raise ValueError(f"metadata case {qid} needs groups and no evidence spans")
        if status in {"corpus_missing", "unanswerable"} and (groups or requirements):
            raise ValueError(f"non-evaluable case {qid} cannot carry gold evidence")

        cases[qid] = GoldCase(
            qid=qid,
            status=status,
            reason=str(raw.get("reason") or ""),
            content_groups=tuple(groups),
            evidence_requirements=tuple(requirements),
        )
    return cases


def matching_group(
    content_sha256: str, groups: Iterable[ContentGroup], used: set[str] | None = None,
) -> ContentGroup | None:
    used = used or set()
    return next(
        (
            group for group in groups
            if group.group_id not in used and content_sha256 in group.content_sha256
        ),
        None,
    )


def ranges_overlap(left_start: int, left_end: int, right_start: int, right_end: int) -> bool:
    return max(left_start, right_start) < min(left_end, right_end)


def packed_span_matches(
    gold: GoldSpan, *, content_sha256: str, chunk_text_hash: str,
    chunk_start_offset: int, chunk_end_offset: int,
    document_start_offset: int | None, document_end_offset: int | None,
) -> bool:
    if content_sha256 != gold.content_sha256:
        return False
    if (
        document_start_offset is not None
        and document_end_offset is not None
        and gold.document_start_offset is not None
        and gold.document_end_offset is not None
    ):
        return ranges_overlap(
            document_start_offset,
            document_end_offset,
            gold.document_start_offset,
            gold.document_end_offset,
        )
    return (
        chunk_text_hash == gold.chunk_text_hash
        and ranges_overlap(
            chunk_start_offset,
            chunk_end_offset,
            gold.chunk_start_offset,
            gold.chunk_end_offset,
        )
    )
