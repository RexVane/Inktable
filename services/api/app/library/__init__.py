"""Ordo AI Library domain layer.

The physical filesystem remains the source of truth.  Everything in this
package is derived knowledge metadata and may be rebuilt from ``contents`` and
its source ``files`` without touching user files.
"""

from app.library.core import (
    ensure_library_schema,
    get_library_item,
    list_library_items,
    mark_library_item_stale,
    replace_library_item_tags,
    set_related_items,
    sync_library_items,
    update_enrichment,
)

__all__ = [
    "ensure_library_schema",
    "get_library_item",
    "list_library_items",
    "mark_library_item_stale",
    "replace_library_item_tags",
    "set_related_items",
    "sync_library_items",
    "update_enrichment",
]
