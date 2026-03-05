from __future__ import annotations

import pytest

from zobs import selector


def test_resolve_collection_key() -> None:
    class FakeZotero:
        def collections(self):
            return [{"data": {"name": "Papers", "key": "ZXCVBN12"}}]

    zot = FakeZotero()
    assert selector.resolve_collection_key(zot, "AB12CD34") == "AB12CD34"
    assert selector.resolve_collection_key(zot, "Papers") == "ZXCVBN12"


def test_parse_selector_tag_and_filtering_and_logic() -> None:
    class FakeZotero:
        def items(self, tag=None, itemType=None):
            assert tag == "ML"
            return [
                {"data": {"key": "A", "tags": [{"tag": "ML"}, {"tag": "NLP"}]}},
                {"data": {"key": "B", "tags": [{"tag": "ml"}]}},
                {"data": {"key": "C", "tags": [{"tag": "NLP"}]}},
            ]

    sel = selector.parse_selector({"ZOTERO_SYNC_MODE": "tag", "ZOTERO_TAG": "ML, NLP"})
    items = sel.fetch_items(FakeZotero(), "journalArticle")
    assert [i["data"]["key"] for i in items] == ["A"]


def test_parse_selector_rejects_invalid_mode() -> None:
    with pytest.raises(selector.SelectorError):
        selector.parse_selector({"ZOTERO_SYNC_MODE": "wat"})


def test_parse_selector_requires_collection_when_mode_collection() -> None:
    with pytest.raises(selector.SelectorError):
        selector.parse_selector({"ZOTERO_SYNC_MODE": "collection"})


def test_parse_selector_requires_tag_when_mode_tag() -> None:
    with pytest.raises(selector.SelectorError):
        selector.parse_selector({"ZOTERO_SYNC_MODE": "tag"})


def test_parse_selector_rejects_blank_tag_list() -> None:
    with pytest.raises(selector.SelectorError):
        selector.parse_selector({"ZOTERO_SYNC_MODE": "tag", "ZOTERO_TAG": " ,  "})


def test_tag_selector_single_tag_returns_all_matching() -> None:
    class FakeZotero:
        def items(self, tag=None, itemType=None):
            assert tag == "ML"
            return [
                {"data": {"key": "A", "tags": [{"tag": "ML"}]}},
                {"data": {"key": "B", "tags": [{"tag": "NLP"}]}},
            ]

    sel = selector.parse_selector({"ZOTERO_SYNC_MODE": "tag", "ZOTERO_TAG": "ML"})
    items = sel.fetch_items(FakeZotero(), "journalArticle")
    assert [i["data"]["key"] for i in items] == ["A", "B"]


def test_tag_selector_handles_missing_tags_field() -> None:
    class FakeZotero:
        def items(self, tag=None, itemType=None):
            assert tag == "ML"
            return [
                {"data": {"key": "A"}},
                {"data": {"key": "B", "tags": [{"tag": "ML"}]}},
            ]

    sel = selector.parse_selector({"ZOTERO_SYNC_MODE": "tag", "ZOTERO_TAG": "ML, NLP"})
    items = sel.fetch_items(FakeZotero(), "journalArticle")
    assert [i["data"]["key"] for i in items] == []
