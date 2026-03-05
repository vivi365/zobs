"""
Selection logic for which Zotero items to sync.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from pyzotero import errors as zotero_errors


class SelectorError(ValueError):
    def __init__(self, message: str, missing: list[str] | None = None) -> None:
        super().__init__(message)
        self.missing = missing or []


class ZoteroClient(Protocol):
    def collections(self) -> list[dict]:
        raise NotImplementedError

    def collection_items(
        self, collection_key: str, itemType: str | None = None
    ) -> list[dict]:
        raise NotImplementedError

    def items(self, tag: str | None = None, itemType: str | None = None) -> list[dict]:
        raise NotImplementedError

    def children(self, zotero_key: str, itemType: str | None = None) -> list[dict]:
        raise NotImplementedError


class ItemSelector(Protocol):
    def fetch_items(self, zot: ZoteroClient, item_types: str) -> list[dict]:
        raise NotImplementedError


def _is_missing(value: str | None) -> bool:
    if not value or not value.strip():
        return True
    return value.lstrip().startswith("#")


def parse_selector(env: Mapping[str, str | None]) -> ItemSelector:
    mode_raw = (env.get("ZOTERO_SYNC_MODE") or "collection").strip().lower()
    if mode_raw not in {"collection", "tag"}:
        raise SelectorError(
            f"Invalid ZOTERO_SYNC_MODE '{mode_raw}'. Expected 'collection' or 'tag'."
        )

    if mode_raw == "collection":
        value = env.get("ZOTERO_COLLECTION")
        if _is_missing(value):
            raise SelectorError(
                "Missing required .env variables: ZOTERO_COLLECTION",
                missing=["ZOTERO_COLLECTION"],
            )
        return CollectionSelector(name_or_key=value.strip())

    value = env.get("ZOTERO_TAG")
    if _is_missing(value):
        raise SelectorError(
            "Missing required .env variables: ZOTERO_TAG",
            missing=["ZOTERO_TAG"],
        )
    tags = [t.strip() for t in value.split(",") if t.strip()]
    if not tags:
        raise SelectorError("ZOTERO_TAG must include at least one tag.")
    return TagSelector(tags=tags)


def resolve_collection_key(zot: ZoteroClient, name_or_key: str) -> str:
    """Accept either a collection name or 8-char key; return the key."""
    if len(name_or_key) == 8 and name_or_key.isalnum():
        return name_or_key
    try:
        collections = zot.collections()
    except zotero_errors.HTTPError as e:
        raise RuntimeError(f"Zotero API error while listing collections: {e}") from e
    matches = [
        c for c in collections if c["data"]["name"].lower() == name_or_key.lower()
    ]
    if not matches:
        hint = "Use the 8-char collection ID (from the URL) or the exact name."
        raise ValueError(f"Collection '{name_or_key}' not found. {hint}")
    if len(matches) > 1:
        raise ValueError(
            f"Multiple collections named '{name_or_key}'. Use the 8-char key instead."
        )
    return matches[0]["data"]["key"]


@dataclass(frozen=True)
class CollectionSelector:
    name_or_key: str

    def fetch_items(self, zot: ZoteroClient, item_types: str) -> list[dict]:
        collection_key = resolve_collection_key(zot, self.name_or_key)
        return zot.collection_items(collection_key, itemType=item_types)


@dataclass(frozen=True)
class TagSelector:
    tags: list[str]

    def fetch_items(self, zot: ZoteroClient, item_types: str) -> list[dict]:
        tags_lower = [t.lower() for t in self.tags]
        first = self.tags[0]
        items = zot.items(tag=first, itemType=item_types)
        if len(tags_lower) == 1:
            return items

        def has_all_tags(item: dict) -> bool:
            item_tags = {
                str(t.get("tag", "")).lower()
                for t in item.get("data", {}).get("tags", [])
            }
            return all(t in item_tags for t in tags_lower)

        return [item for item in items if has_all_tags(item)]
