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


def test_collection_selector_splits_top_level_items_and_children() -> None:
    class FakeZotero:
        def collections(self):
            return []

        def collection_items(self, collection_key, itemType=None):
            assert collection_key == "COLL0001"
            return [
                {"data": {"key": "P1", "itemType": "journalArticle"}},
                {"data": {"key": "P2", "itemType": "conferencePaper"}},
                {"data": {"key": "X9", "itemType": "blogPost"}},  # unwanted type
                {"data": {"key": "A1", "itemType": "attachment", "parentItem": "P1"}},
                {"data": {"key": "N1", "itemType": "note", "parentItem": "P1"}},
                {"data": {"key": "A2", "itemType": "attachment", "parentItem": "P2"}},
            ]

        def everything(self, result):
            return result

    sel = selector.parse_selector(
        {"ZOTERO_SYNC_MODE": "collection", "ZOTERO_COLLECTION": "COLL0001"}
    )
    items, children = sel.fetch_items(FakeZotero(), "journalArticle || conferencePaper")

    assert sorted(i["data"]["key"] for i in items) == ["P1", "P2"]
    assert {c["data"]["key"] for c in children["P1"]} == {"A1", "N1"}
    assert {c["data"]["key"] for c in children["P2"]} == {"A2"}


def test_parse_selector_tag_and_filtering_and_logic() -> None:
    class FakeZotero:
        def items(self, tag=None, itemType=None):
            if itemType == "attachment || note":
                return [
                    {
                        "data": {
                            "key": "att1",
                            "itemType": "attachment",
                            "parentItem": "A",
                        }
                    },
                    {
                        "data": {
                            "key": "att2",
                            "itemType": "attachment",
                            "parentItem": "C",
                        }
                    },
                ]
            assert tag == "ML"
            return [
                {"data": {"key": "A", "tags": [{"tag": "ML"}, {"tag": "NLP"}]}},
                {"data": {"key": "B", "tags": [{"tag": "ml"}]}},
                {"data": {"key": "C", "tags": [{"tag": "NLP"}]}},
            ]

        def everything(self, result):
            return result

    sel = selector.parse_selector({"ZOTERO_SYNC_MODE": "tag", "ZOTERO_TAG": "ML, NLP"})
    items, children = sel.fetch_items(FakeZotero(), "journalArticle")
    assert [i["data"]["key"] for i in items] == ["A"]
    # only children of selected parents are kept ("C" was filtered out)
    assert list(children) == ["A"]


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
            if itemType == "attachment || note":
                return []
            assert tag == "ML"
            return [
                {"data": {"key": "A", "tags": [{"tag": "ML"}]}},
                {"data": {"key": "B", "tags": [{"tag": "NLP"}]}},
            ]

        def everything(self, result):
            return result

    sel = selector.parse_selector({"ZOTERO_SYNC_MODE": "tag", "ZOTERO_TAG": "ML"})
    items, _ = sel.fetch_items(FakeZotero(), "journalArticle")
    assert [i["data"]["key"] for i in items] == ["A", "B"]


def test_tag_selector_handles_missing_tags_field() -> None:
    class FakeZotero:
        def items(self, tag=None, itemType=None):
            if itemType == "attachment || note":
                return []
            assert tag == "ML"
            return [
                {"data": {"key": "A"}},
                {"data": {"key": "B", "tags": [{"tag": "ML"}]}},
            ]

        def everything(self, result):
            return result

    sel = selector.parse_selector({"ZOTERO_SYNC_MODE": "tag", "ZOTERO_TAG": "ML, NLP"})
    items, _ = sel.fetch_items(FakeZotero(), "journalArticle")
    assert [i["data"]["key"] for i in items] == []
