from __future__ import annotations

import argparse

from zobs import augment
from zobs.metadata import Metadata, Resolution


def _args(**over):
    base = dict(
        select=None,
        all=False,
        include_complete=False,
        as_json=False,
        dry_run=False,
        yes=False,
        no_zotero=False,
        no_bib=False,
        overwrite=False,
        no_repair=False,
        min_confidence=93.0,
        sources="crossref",
        refresh=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


# ── completeness model ──────────────────────────────────────────────────────


def test_missing_fields_by_type() -> None:
    journal = {"itemType": "journalArticle", "title": "T", "date": "2020"}
    assert set(augment.missing_fields(journal)) == {"creators", "venue", "pages"}

    conf = {
        "itemType": "conferencePaper",
        "title": "T",
        "date": "2020",
        "pages": "1-9",
        "proceedingsTitle": "Proc",
        "creators": [{"creatorType": "author", "lastName": "X"}],
    }
    assert augment.missing_fields(conf) == []


def test_malformed_detects_bad_year_and_pages() -> None:
    assert augment.malformed_fields({"date": "June"}) == ["date"]
    assert augment.malformed_fields({"date": "2020", "pages": "n. pag."}) == ["pages"]
    assert augment.malformed_fields({"date": "2020-06", "pages": "10-20"}) == []


def test_preprint_with_arxiv_id_is_complete() -> None:
    data = {
        "itemType": "preprint",
        "title": "T",
        "date": "2026",
        "archiveID": "arXiv:2607.12316",
        "creators": [{"creatorType": "author", "lastName": "X"}],
    }
    assert augment.missing_fields(data) == []


# ── plan_changes ────────────────────────────────────────────────────────────


def _plan(data, meta, tier="exact", **over):
    cand = augment.Candidate(
        key="K",
        citekey="ck",
        item_type=data["itemType"],
        title=data.get("title", ""),
        doi=str(data.get("DOI") or ""),
        missing=augment.missing_fields(data),
        malformed=augment.malformed_fields(data),
        item={"key": "K", "version": 1, "data": data},
    )
    return augment.plan_changes(cand, Resolution(meta, tier), _args(**over))


def test_plan_fills_blanks_only() -> None:
    data = {
        "itemType": "journalArticle",
        "title": "T",
        "date": "",
        "publicationTitle": "Existing Journal",
        "creators": [{"creatorType": "author", "lastName": "X"}],
    }
    meta = Metadata(
        container_title="Canonical Journal", volume="4", pages="1-9", year=2021
    )
    plan = _plan(data, meta)
    assert plan.changes["volume"] == ("", "4")  # filled even though not "required"
    assert plan.changes["date"] == ("", "2021")
    assert "publicationTitle" not in plan.changes  # already had a value


def test_plan_overwrite_replaces_on_exact() -> None:
    data = {
        "itemType": "journalArticle",
        "title": "T",
        "date": "2021",
        "publicationTitle": "Old",
        "creators": [{"creatorType": "author", "lastName": "X"}],
    }
    meta = Metadata(container_title="New", year=2021)
    plan = _plan(data, meta, overwrite=True)
    assert plan.changes["publicationTitle"] == ("Old", "New")


def test_plan_repairs_malformed_year() -> None:
    data = {
        "itemType": "conferencePaper",
        "title": "T",
        "date": "June",
        "proceedingsTitle": "P",
        "pages": "1-9",
        "creators": [{"creatorType": "author", "lastName": "X"}],
    }
    plan = _plan(data, Metadata(year=2025, month=6))
    assert plan.changes["date"] == ("June", "2025-06")

    plan_norepair = _plan(data, Metadata(year=2025, month=6), no_repair=True)
    assert "date" not in plan_norepair.changes


def test_plan_none_metadata_no_changes() -> None:
    data = {"itemType": "journalArticle", "title": "T", "date": ""}
    plan = _plan(data, None)
    assert plan.changes == {}
    assert plan.still_missing  # unchanged, still incomplete


# ── bib splice ──────────────────────────────────────────────────────────────

_BIB = """@article{alpha,
  title   = {Alpha},
  author  = {A},
  year    = {},
  journal = {},
  doi     = {},
}

@article{beta,
  title   = {Beta},
  author  = {B},
  year    = {2020},
  journal = {J},
  doi     = {},
}
"""


def test_splice_replaces_single_entry() -> None:
    new = "@article{alpha,\n  title = {Alpha},\n  year  = {2019},\n}\n"
    out = augment._splice_bib(_BIB, "alpha", new, None)
    assert "year  = {2019}" in out
    assert out.count("@article{beta,") == 1
    assert "@article{alpha," in out and out.count("@article{alpha,") == 1


def test_splice_adds_and_replaces_marker() -> None:
    new = "@article{alpha,\n  title = {Alpha},\n}\n"
    once = augment._splice_bib(_BIB, "alpha", new, "zobs: no confident match")
    assert "% zobs: no confident match\n@article{alpha," in once
    # a second splice must not stack markers
    twice = augment._splice_bib(once, "alpha", new, "zobs: still nothing")
    assert twice.count("% zobs:") == 1
    assert "still nothing" in twice


def test_splice_appends_when_missing() -> None:
    new = "@article{gamma,\n  title = {Gamma},\n}\n"
    out = augment._splice_bib(_BIB, "gamma", new, None)
    assert out.endswith("@article{gamma,\n  title = {Gamma},\n}\n")
    assert "@article{alpha," in out
