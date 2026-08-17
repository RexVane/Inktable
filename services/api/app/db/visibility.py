"""Shared SQL predicates for files visible to users and QA."""

from __future__ import annotations

# Callers normally alias files as ``f`` and sources as ``s``.  Keep the
# predicate LEFT-JOIN safe: source_id IS NULL represents an orphan/preserved
# record that remains visible by the existing product contract.
VISIBLE_FILES_COND = (
    "(f.source_id IS NULL OR s.enabled = 1) "
    "AND f.state != 'ignored' "
    "AND (f.state != 'missing' OR COALESCE(f.preserved_path, '') != '')"
)


def visible_files_condition(file_alias: str = "f", source_alias: str = "s") -> str:
    return VISIBLE_FILES_COND.replace("f.", f"{file_alias}.").replace(
        "s.", f"{source_alias}."
    )


def visible_content_exists(
    content_expr: str = "ch.content_id",
    file_alias: str = "vf",
    source_alias: str = "vs",
) -> str:
    """Return an EXISTS predicate for a content with a visible replica."""
    cond = VISIBLE_FILES_COND.replace("f.", f"{file_alias}.").replace(
        "s.", f"{source_alias}."
    )
    return (
        "(NOT EXISTS (SELECT 1 FROM files any_file "
        "WHERE any_file.content_id = {content}) OR "
        "EXISTS (SELECT 1 FROM files {file} "
        "LEFT JOIN sources {source} ON {source}.id = {file}.source_id "
        "WHERE {file}.content_id = {content} AND {cond}))"
    ).format(
        file=file_alias,
        source=source_alias,
        content=content_expr,
        cond=cond,
    )
