from __future__ import annotations

from pathlib import Path

import pytest

import zobs.sync as sync
from zobs import selector


def test_slugify_basic() -> None:
    assert sync.slugify("A Study: on / test?") == "A_Study_on__test"
    assert len(sync.slugify("a" * 200)) == 80


def test_parse_frontmatter() -> None:
    text = "---\nfoo: bar\nnum: 3\n---\nBody\n"
    assert sync.parse_frontmatter(text) == {"foo": "bar", "num": 3}
    assert sync.parse_frontmatter("No frontmatter") == {}


def test_scan_obsidian_notes_indexes_only_opted_in(tmp_path: Path) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "in.md").write_text("---\ncitekey: Foo2020\nzotero_key: AB12CD34\n---\n")
    (notes / "out.md").write_text("---\ncitekey: Bar2021\n---\n")

    index = sync.scan_obsidian_notes(notes)

    assert index == {"AB12CD34": (notes / "in.md", "Foo2020")}


def test_resolve_collection_key() -> None:
    class FakeZotero:
        def collections(self):
            return [{"data": {"name": "Papers", "key": "ZXCVBN12"}}]

    zot = FakeZotero()
    assert selector.resolve_collection_key(zot, "AB12CD34") == "AB12CD34"
    assert selector.resolve_collection_key(zot, "Papers") == "ZXCVBN12"


def test_load_config_rejects_blank_required(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ZOTERO_USER_ID", "123")
    monkeypatch.setenv("ZOTERO_API_KEY", "abc")
    monkeypatch.setenv("ZOTERO_COLLECTION", "   ")

    with pytest.raises(SystemExit):
        sync.load_config()

    out = capsys.readouterr().out
    assert "Missing required .env variables" in out


def test_parse_selector_tag_and_filtering_and_logic() -> None:
    class FakeZotero:
        def items(self, tag=None, itemType=None):
            assert tag == "ML"
            return [
                {"data": {"key": "A", "tags": [{"tag": "ML"}, {"tag": "NLP"}]}},
                {"data": {"key": "B", "tags": [{"tag": "ml"}]}},
                {"data": {"key": "C", "tags": [{"tag": "NLP"}]}},
            ]

    sel = selector.parse_selector(
        {"ZOTERO_SYNC_MODE": "tag", "ZOTERO_TAG": "ML, NLP"}
    )
    items = sel.fetch_items(FakeZotero(), "journalArticle")
    assert [i["data"]["key"] for i in items] == ["A"]


def test_citation_key_from_item_prefers_bbt_then_extra_then_fallback() -> None:
    data = {"citationKey": "ZeroRootCauseAnalyses"}
    assert sync.citation_key_from_item(data, "AB12CD34") == "ZeroRootCauseAnalyses"

    data = {"extra": "Citation Key: FromExtra\nOther: Value"}
    assert sync.citation_key_from_item(data, "AB12CD34") == "FromExtra"

    data = {"extra": "Other: Value"}
    assert sync.citation_key_from_item(data, "AB12CD34") == "AB12CD34"


def test_main_sync_with_obsidian_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeZotero:
        def __init__(self, *args, **kwargs):
            pass

        def collections(self):
            return [{"data": {"name": "Papers", "key": "COLL0001"}}]

        def collection_items(self, collection_key, itemType=None):
            assert collection_key == "COLL0001"
            return [
                {
                    "data": {
                        "title": "Great Paper",
                        "key": "AB12CD34",
                        "creators": [
                            {
                                "creatorType": "author",
                                "firstName": "Ada",
                                "lastName": "Lovelace",
                            }
                        ],
                        "date": "2020-01-01",
                        "publicationTitle": "Journal",
                        "DOI": "10.1000/test",
                    }
                }
            ]

        def children(self, zotero_key, itemType=None):
            if itemType == "attachment":
                return [{"data": {"contentType": "application/pdf", "key": "ATTACH01"}}]
            if itemType == "note":
                return []
            raise AssertionError("Unexpected itemType")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sync.zotero, "Zotero", FakeZotero)

    storage = tmp_path / "Zotero" / "storage" / "ATTACH01"
    storage.mkdir(parents=True)
    (storage / "paper.pdf").write_text("pdf")

    notes_root = tmp_path / "obsidian"
    notes_root.mkdir()
    note = notes_root / "great.md"
    note.write_text("---\ncitekey: Lovelace2020\nzotero_key: AB12CD34\n---\n")

    monkeypatch.setenv("ZOTERO_USER_ID", "123")
    monkeypatch.setenv("ZOTERO_API_KEY", "abc")
    monkeypatch.setenv("ZOTERO_COLLECTION", "Papers")
    monkeypatch.setenv("ZOTERO_STORAGE", str(tmp_path / "Zotero" / "storage"))
    monkeypatch.setenv("OBSIDIAN_NOTES", str(notes_root))

    sync.main()

    papers_dir = tmp_path / "references" / "papers"
    notes_dir = tmp_path / "references" / "notes"
    bib = tmp_path / "references" / "refs.bib"

    linked = list(papers_dir.iterdir())
    assert len(linked) == 1
    assert linked[0].name.startswith("Lovelace2020_")
    assert linked[0].is_symlink()

    note_link = notes_dir / "obsidian" / "Lovelace2020.md"
    assert note_link.is_symlink()

    bib_text = bib.read_text()
    assert "@article{Lovelace2020" in bib_text
    assert "Great Paper" in bib_text


def _fake_zotero(item_key: str, title: str, attach_key: str):
    """Build a minimal FakeZotero class for a single-item collection."""

    class Fake:
        def __init__(self, *a, **kw):
            pass

        def collections(self):
            return []

        def collection_items(self, coll, itemType=None):
            return [
                {
                    "data": {
                        "title": title,
                        "key": item_key,
                        "creators": [],
                        "date": "2020",
                        "publicationTitle": "",
                        "DOI": "",
                    }
                }
            ]

        def children(self, key, itemType=None):
            if itemType == "attachment":
                return [{"data": {"contentType": "application/pdf", "key": attach_key}}]
            return []

    return Fake


def test_cleanup_removes_stale_symlinks_on_collection_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Switching collection removes old PDFs and Obsidian note symlinks."""
    storage = tmp_path / "Zotero" / "storage"
    for key, fname in [("ATCHA001", "a.pdf"), ("ATCHB001", "b.pdf")]:
        (storage / key).mkdir(parents=True)
        (storage / key / fname).write_text("pdf")

    notes_root = tmp_path / "obsidian"
    notes_root.mkdir()
    (notes_root / "note_a.md").write_text(
        "---\ncitekey: AuthorA2020\nzotero_key: AAAAAAAA\n---\n"
    )
    (notes_root / "note_b.md").write_text(
        "---\ncitekey: AuthorB2021\nzotero_key: BBBBBBBB\n---\n"
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ZOTERO_USER_ID", "123")
    monkeypatch.setenv("ZOTERO_API_KEY", "abc")
    monkeypatch.setenv("ZOTERO_STORAGE", str(storage))
    monkeypatch.setenv("OBSIDIAN_NOTES", str(notes_root))

    papers_dir = tmp_path / "references" / "papers"
    obsidian_dir = tmp_path / "references" / "notes" / "obsidian"

    # Run 1: collection A
    monkeypatch.setenv("ZOTERO_COLLECTION", "AAAAAAAA")
    monkeypatch.setattr(
        sync.zotero, "Zotero", _fake_zotero("AAAAAAAA", "Paper A", "ATCHA001")
    )
    sync.main()

    assert any(p.name.startswith("AuthorA2020_") for p in papers_dir.iterdir())
    assert (obsidian_dir / "AuthorA2020.md").is_symlink()

    # Run 2: collection B — A's artefacts should be gone
    monkeypatch.setenv("ZOTERO_COLLECTION", "BBBBBBBB")
    monkeypatch.setattr(
        sync.zotero, "Zotero", _fake_zotero("BBBBBBBB", "Paper B", "ATCHB001")
    )
    sync.main()

    pdf_names = [p.name for p in papers_dir.iterdir()]
    assert not any(
        n.startswith("AuthorA2020_") for n in pdf_names
    ), "stale PDF not removed"
    assert any(n.startswith("AuthorB2021_") for n in pdf_names), "new PDF not linked"
    assert not (
        obsidian_dir / "AuthorA2020.md"
    ).exists(), "stale note symlink not removed"
    assert (obsidian_dir / "AuthorB2021.md").is_symlink(), "new note not linked"


def test_cleanup_removes_broken_symlink_when_obsidian_note_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting an Obsidian note removes its dangling symlink on the next run."""
    storage = tmp_path / "Zotero" / "storage" / "ATCHA001"
    storage.mkdir(parents=True)
    (storage / "a.pdf").write_text("pdf")

    notes_root = tmp_path / "obsidian"
    notes_root.mkdir()
    note = notes_root / "note_a.md"
    note.write_text("---\ncitekey: AuthorA2020\nzotero_key: AAAAAAAA\n---\n")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ZOTERO_USER_ID", "123")
    monkeypatch.setenv("ZOTERO_API_KEY", "abc")
    monkeypatch.setenv("ZOTERO_COLLECTION", "AAAAAAAA")
    monkeypatch.setenv("ZOTERO_STORAGE", str(tmp_path / "Zotero" / "storage"))
    monkeypatch.setenv("OBSIDIAN_NOTES", str(notes_root))
    monkeypatch.setattr(
        sync.zotero, "Zotero", _fake_zotero("AAAAAAAA", "Paper A", "ATCHA001")
    )

    sync.main()

    obsidian_dir = tmp_path / "references" / "notes" / "obsidian"
    assert (obsidian_dir / "AuthorA2020.md").is_symlink()

    # Delete the source note — symlink becomes dangling
    note.unlink()
    assert not (obsidian_dir / "AuthorA2020.md").exists()  # confirm it's broken

    sync.main()

    assert not (
        obsidian_dir / "AuthorA2020.md"
    ).exists(), "broken symlink not cleaned up"
    assert not (
        obsidian_dir / "AuthorA2020.md"
    ).is_symlink(), "dangling symlink still present"


def test_main_sync_fallback_to_zotero_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeZotero:
        def __init__(self, *args, **kwargs):
            pass

        def collections(self):
            return [{"data": {"name": "Papers", "key": "COLL0002"}}]

        def collection_items(self, collection_key, itemType=None):
            assert collection_key == "COLL0002"
            return [
                {
                    "data": {
                        "title": "Fallback Note Paper",
                        "key": "ZZ99YY88",
                        "creators": [],
                        "date": "2019",
                        "publicationTitle": "",
                        "DOI": "",
                    }
                }
            ]

        def children(self, zotero_key, itemType=None):
            if itemType == "attachment":
                return [{"data": {"contentType": "application/pdf", "key": "ATTACH02"}}]
            if itemType == "note":
                return [{"data": {"note": "<p>Hi<br/>there</p>"}}]
            raise AssertionError("Unexpected itemType")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sync.zotero, "Zotero", FakeZotero)

    storage = tmp_path / "Zotero" / "storage" / "ATTACH02"
    storage.mkdir(parents=True)
    (storage / "paper.pdf").write_text("pdf")

    monkeypatch.setenv("ZOTERO_USER_ID", "123")
    monkeypatch.setenv("ZOTERO_API_KEY", "abc")
    monkeypatch.setenv("ZOTERO_COLLECTION", "Papers")
    monkeypatch.setenv("ZOTERO_STORAGE", str(tmp_path / "Zotero" / "storage"))
    monkeypatch.delenv("OBSIDIAN_NOTES", raising=False)

    sync.main()

    notes_dir = tmp_path / "references" / "notes"
    note_file = notes_dir / "zotero" / "ZZ99YY88.md"
    assert note_file.exists()
    text = note_file.read_text()
    assert "Imported from Zotero" in text
    assert "Hi\nthere" in text
