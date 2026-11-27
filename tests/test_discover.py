from __future__ import annotations

import argparse
import json

import pytest

from zobs import discover
from zobs.metadata import Metadata


def _args(**over):
    base = dict(
        limit=25, min_count=2, as_json=False, refresh=False, ignore=None, last=False
    )
    base.update(over)
    return argparse.Namespace(**base)


class FakeCache:
    """Serves canned JSON keyed by a substring of the request URL."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.refresh = False
        self.urls: list[str] = []

    def fetch_json(self, url, **kw):
        self.urls.append(url)
        for frag, payload in self.routes.items():
            if frag in url:
                return payload
        return None

    fetch_text = fetch_json


def _work(wid, title, *, refs=(), cited=0, doi=None, author="Woo", year=2020):
    return {
        "id": f"https://openalex.org/{wid}",
        "title": title,
        "publication_year": year,
        "cited_by_count": cited,
        "doi": f"https://doi.org/{doi}" if doi else None,
        "type": "article",
        "authorships": [{"author": {"display_name": f"Seunghoon {author}"}}],
        "referenced_works": [f"https://openalex.org/{r}" for r in refs],
    }


# ── resolving the selection to OpenAlex works ───────────────────────────────


def test_resolve_by_doi_carries_reference_list() -> None:
    cache = FakeCache(
        {
            # the DOI is percent-escaped into the path, so match on the prefix
            "works/doi:10.1109": _work(
                "W1", "VUDDY", refs=["W7", "W8"], cited=400, doi="10.1109/sp.2017.62"
            )
        }
    )
    work = discover.resolve_work({"DOI": "10.1109/SP.2017.62", "title": "VUDDY"}, cache)
    assert work.openalex_id == "W1"
    assert work.referenced_works == ["W7", "W8"]
    # one request: the DOI lookup already returned the bibliography
    assert len(cache.urls) == 1


def test_resolve_rejects_low_tier_title_match() -> None:
    # Same first-author surname and overlapping words, but the "MOVERY" key term
    # is missing from the candidate, so this is the VUDDY paper, not ours.
    cache = FakeCache(
        {
            "title.search": {
                "results": [
                    _work(
                        "W9",
                        "VUDDY: A Scalable Approach for Vulnerable Code Clone Discovery",
                        refs=["W7"],
                        year=2017,
                    )
                ]
            }
        }
    )
    data = {
        "title": "MOVERY: A Precise Approach for Modified Vulnerable Code Clone Detection",
        "date": "2022",
        "creators": [
            {"creatorType": "author", "firstName": "Seunghoon", "lastName": "Woo"}
        ],
    }
    assert discover.resolve_work(data, cache) is None


def test_resolve_accepts_high_tier_title_match() -> None:
    title = "MOVERY: A Precise Approach for Modified Vulnerable Code Clone Detection"
    cache = FakeCache({"title.search": {"results": [_work("W2", title, refs=["W7"])]}})
    data = {
        "title": title,
        "date": "2020",
        "creators": [
            {"creatorType": "author", "firstName": "Seunghoon", "lastName": "Woo"}
        ],
    }
    work = discover.resolve_work(data, cache)
    assert work.openalex_id == "W2"


def test_build_index_records_which_papers_cite_what() -> None:
    held = [
        discover.Held("alpha", "K1", "Alpha", "", "W1", ["W7", "W8"]),
        discover.Held("beta", "K2", "Beta", "", "W2", ["W7"]),
        discover.Held("gamma", "K3", "Gamma", "", None, []),
    ]
    index = discover.build_index(held)
    assert index["W7"] == {"alpha", "beta"}
    assert index["W8"] == {"alpha"}


# ── ranking ─────────────────────────────────────────────────────────────────


def test_rank_score_prefers_the_niche_paper() -> None:
    niche = discover.rank_score(5, 200)
    famous = discover.rank_score(5, 40_000)
    assert niche > famous
    # local evidence still leads: twice the citing papers beats the penalty
    assert discover.rank_score(10, 40_000) > niche
    # an unknown global count is not punished
    assert discover.rank_score(5, None) == 5.0


def _rank(index, cache, **over):
    kw = dict(
        held_ids=set(),
        held_dois=set(),
        ignore=set(),
        min_count=2,
        limit=25,
    )
    kw.update(over)
    return discover.rank(index, cache, **kw)


_TWO_WORKS = {
    "openalex_id:": {
        "results": [
            _work("W7", "A Famous Textbook", cited=40_000, doi="10.1/famous"),
            _work("W8", "A Niche Workshop Paper", cited=200, doi="10.1/niche"),
        ]
    }
}


def test_rank_puts_the_niche_paper_above_the_famous_one() -> None:
    cache = FakeCache(_TWO_WORKS)
    rows = _rank({"W7": {"a", "b", "c"}, "W8": {"a", "b", "c"}}, cache)
    assert [r.openalex_id for r in rows] == ["W8", "W7"]
    assert rows[0].citekeys == ["a", "b", "c"]  # evidence travels with the row


def test_rank_drops_thin_evidence_below_min_count() -> None:
    cache = FakeCache(_TWO_WORKS)
    rows = _rank({"W7": {"a", "b", "c"}, "W8": {"a"}}, cache)
    assert [r.openalex_id for r in rows] == ["W7"]


def test_rank_excludes_papers_already_held() -> None:
    cache = FakeCache(_TWO_WORKS)
    index = {"W7": {"a", "b"}, "W8": {"a", "b"}}

    by_id = _rank(index, cache, held_ids={"W8"})
    assert [r.openalex_id for r in by_id] == ["W7"]

    # the same paper reached by DOI instead: it is only recognised after the
    # metadata fetch, but it must still not be suggested
    by_doi = _rank(index, cache, held_dois={"10.1/niche"})
    assert [r.openalex_id for r in by_doi] == ["W7"]


def test_rank_honours_the_ignore_list() -> None:
    cache = FakeCache(_TWO_WORKS)
    index = {"W7": {"a", "b"}, "W8": {"a", "b"}}
    assert [r.openalex_id for r in _rank(index, cache, ignore={"w8"})] == ["W7"]
    assert [r.openalex_id for r in _rank(index, cache, ignore={"10.1/famous"})] == [
        "W8"
    ]


def test_rank_applies_the_limit() -> None:
    cache = FakeCache(_TWO_WORKS)
    rows = _rank({"W7": {"a", "b"}, "W8": {"a", "b"}}, cache, limit=1)
    assert len(rows) == 1


# ── batching ────────────────────────────────────────────────────────────────


def test_metadata_fetch_is_batched_fifty_ids_at_a_time() -> None:
    ids = [f"W{i}" for i in range(1, 61)]
    cache = FakeCache({"openalex_id:": {"results": []}})
    _rank({i: {"a", "b"} for i in ids}, cache)
    assert len(cache.urls) == 2
    first, second = (u.split("openalex_id:")[1].split("&")[0] for u in cache.urls)
    assert len(first.split("|")) == 50
    assert len(second.split("|")) == 10


# ── ignore file ─────────────────────────────────────────────────────────────


def test_normalise_key_strips_the_usual_prefixes() -> None:
    assert discover.normalise_key("https://openalex.org/W123") == "w123"
    assert discover.normalise_key("https://doi.org/10.1145/X") == "10.1145/x"
    assert discover.normalise_key("doi:10.1145/X") == "10.1145/x"
    assert discover.normalise_key("  W123  ") == "w123"


def test_ignore_file_round_trip(tmp_path) -> None:
    path = tmp_path / "references" / ".zobs-ignore"
    assert discover.append_ignore(path, ["W123", "https://doi.org/10.1145/X"]) == [
        "w123",
        "10.1145/x",
    ]
    assert discover.load_ignore(path) == {"w123", "10.1145/x"}
    # a key already listed is not added twice
    assert discover.append_ignore(path, ["openalex.org/W123"]) == []
    assert path.read_text(encoding="utf-8").count("w123") == 1


def test_load_ignore_skips_comments_but_keeps_doi_hashes(tmp_path) -> None:
    path = tmp_path / ".zobs-ignore"
    path.write_text(
        "# a comment\n" "W123   # rejected: survey\n" "\n" "10.1145/has#hash\n",
        encoding="utf-8",
    )
    assert discover.load_ignore(path) == {"w123", "10.1145/has#hash"}


def test_missing_ignore_file_is_empty(tmp_path) -> None:
    assert discover.load_ignore(tmp_path / "nope") == set()


# ── output ──────────────────────────────────────────────────────────────────


def test_render_names_the_citing_papers(capsys) -> None:
    row = discover.Discovery(
        "W8",
        ["alpha", "beta"],
        Metadata(
            title="A Niche Workshop Paper",
            container_title="WOOT",
            year=2019,
            doi="10.1/niche",
            cited_by_count=200,
            authors=[("Seunghoon", "Woo")],
        ),
    )
    cov = discover.Coverage(total=61, resolved=54, with_refs=48, references=1900)
    discover.render([row], cov, _args())
    out = capsys.readouterr().out
    assert "Resolved 54/61 selection items; 48 had reference data" in out
    # the suggestion leads, untruncated, with a link that opens it
    assert "   1. A Niche Workshop Paper" in out
    assert "Woo 2019 · WOOT" in out
    assert "https://doi.org/10.1/niche" in out
    # the citekeys are labelled as the user's own citing papers
    assert "cited by 2 of your papers (200 globally):" in out
    assert "alpha, beta" in out
    assert out.index("A Niche Workshop Paper") < out.index("alpha, beta")


def test_render_keeps_long_titles_and_links_openalex_without_doi(capsys) -> None:
    title = "A Very Long Title " * 8
    row = discover.Discovery(
        "W9",
        ["alpha", "beta"],
        Metadata(title=title.strip(), year=2020, authors=[("A", "B"), ("C", "D")]),
    )
    cov = discover.Coverage(total=2, resolved=2, with_refs=2, references=10)
    discover.render([row], cov, _args())
    out = capsys.readouterr().out
    assert " ".join(out.split()).count("A Very Long Title") == 8  # never clipped
    assert "…" not in out
    assert "B et al. 2020" in out
    assert "https://openalex.org/W9" in out


def test_ignore_accepts_the_printed_link() -> None:
    assert discover.normalise_key("https://doi.org/10.1/Niche") == "10.1/niche"
    assert discover.normalise_key("https://openalex.org/W9") == "w9"


def test_render_says_so_when_there_is_nothing(capsys) -> None:
    cov = discover.Coverage(total=10, resolved=4, with_refs=3, references=20)
    discover.render([], cov, _args())
    out = capsys.readouterr().out
    assert "Nothing cited by 2 or more" in out
    assert "USENIX" in out  # thin coverage is called out, not hidden


def test_json_output_carries_coverage_and_evidence() -> None:
    import json

    row = discover.Discovery(
        "W8",
        ["alpha", "beta"],
        Metadata(title="Niche", doi="10.1/niche", cited_by_count=200, year=2019),
    )
    cov = discover.Coverage(total=61, resolved=54, with_refs=48, references=1900)
    payload = json.loads(discover.as_json([row], cov))
    assert payload["coverage"] == {
        "selected": 61,
        "resolved": 54,
        "withReferences": 48,
        "referencesRead": 1900,
        "duplicates": [],
    }
    assert payload["candidates"][0]["via"] == ["alpha", "beta"]
    assert payload["candidates"][0]["citedByGlobal"] == 200


def test_duplicate_zotero_items_count_once() -> None:
    held = [
        discover.Held("liStudy2017a", "K1", "Study", "", "W1", ["W7", "W8"]),
        discover.Held("liStudy2017", "K2", "Study", "", "W1", ["W7", "W8"]),
        discover.Held("beta", "K3", "Beta", "", "W2", ["W7"]),
        discover.Held("gamma", "K4", "Gamma", "", None, []),
    ]
    unique, dups = discover.dedupe(held)
    # the unsuffixed citekey is kept, the original order otherwise preserved
    assert [h.citekey for h in unique] == ["liStudy2017", "beta", "gamma"]
    assert dups == [["liStudy2017", "liStudy2017a"]]
    index = discover.build_index(unique)
    assert index["W7"] == {"liStudy2017", "beta"}  # 2, not 3
    assert index["W8"] == {"liStudy2017"}  # below --min-count 2 now


def test_render_lists_duplicates(capsys) -> None:
    cov = discover.Coverage(
        total=3,
        resolved=3,
        with_refs=3,
        references=9,
        duplicates=[["liStudy2017", "liStudy2017a"]],
    )
    row = discover.Discovery("W7", ["beta", "liStudy2017"], Metadata(title="T"))
    discover.render([row], cov, _args())
    out = capsys.readouterr().out
    assert "1 paper is in your selection more than once" in out
    assert "liStudy2017 = liStudy2017a" in out


# ── ignoring by row number ──────────────────────────────────────────────────


def test_rank_specs_are_told_apart_from_dois_and_ids() -> None:
    assert discover.is_rank_spec("3")
    assert discover.is_rank_spec("1-25")
    assert discover.is_rank_spec("2,5,7-9")
    assert not discover.is_rank_spec("10.1109/sp.2015.48")
    assert not discover.is_rank_spec("W2888989071")
    assert not discover.is_rank_spec("https://doi.org/10.1/x")
    assert discover.parse_ranks("2,5,7-9") == [2, 5, 7, 8, 9]
    assert discover.parse_ranks("3-1,2") == [1, 2, 3]


def _saved(tmp_path, n=3):
    rows = [
        discover.Discovery(
            f"W{i}", ["a", "b"], Metadata(title=f"Paper {i}", doi=f"10.1/p{i}")
        )
        for i in range(1, n + 1)
    ]
    rows[1].metadata.doi = None  # row 2 is known only by its OpenAlex id
    path = tmp_path / "last.json"
    cov = discover.Coverage(total=5, resolved=5, with_refs=5, references=50)
    discover.save_last(path, rows, cov, 2)
    return path


def test_expand_ignore_maps_rows_to_what_was_printed(tmp_path) -> None:
    last = discover.load_last(_saved(tmp_path))
    picked = discover.expand_ignore(["1-2", "10.9/other"], last)
    assert picked == [
        ("10.1/p1", "Paper 1"),
        ("W2", "Paper 2"),
        ("10.9/other", ""),
    ]


def test_expand_ignore_refuses_rows_it_did_not_show(tmp_path) -> None:
    last = discover.load_last(_saved(tmp_path))
    with pytest.raises(ValueError, match="no row 4"):
        discover.expand_ignore(["2-4"], last)
    with pytest.raises(ValueError, match="no saved list to number from"):
        discover.expand_ignore(["1"], [])


def test_ignore_by_rank_end_to_end(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    _ = _saved(tmp_path)
    (tmp_path / discover.LAST_FILE).parent.mkdir(parents=True)
    (tmp_path / "last.json").rename(tmp_path / discover.LAST_FILE)

    rc = discover.run_discover(_args(ignore=["1-3"]))

    assert rc == 0
    out = capsys.readouterr().out
    assert "3 added to" in out and "Paper 2" in out
    keys = discover.load_ignore(tmp_path / discover.IGNORE_FILE)
    assert keys == {"10.1/p1", "w2", "10.1/p3"}


def test_ignore_by_rank_in_the_wrong_folder_names_the_path(
    tmp_path, monkeypatch, capsys
) -> None:
    # regression: run from a folder with no saved list, the error has to say
    # where it looked, and nothing may be written there
    monkeypatch.chdir(tmp_path)
    rc = discover.run_discover(_args(ignore=["1-25"]))
    assert rc == 1
    err = capsys.readouterr().err
    assert str(tmp_path / discover.LAST_FILE) in err
    assert not (tmp_path / discover.IGNORE_FILE).exists()


# ── --last ──────────────────────────────────────────────────────────────────


def _install_last(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    saved = _saved(tmp_path)
    (tmp_path / discover.LAST_FILE).parent.mkdir(parents=True)
    saved.rename(tmp_path / discover.LAST_FILE)


def test_last_reprints_the_run_as_it_was_shown(tmp_path, monkeypatch, capsys) -> None:
    rows = [
        discover.Discovery(
            "W1",
            ["alpha", "beta"],
            Metadata(
                title="Paper One",
                container_title="IEEE S&P",
                year=2015,
                doi="10.1/p1",
                cited_by_count=181,
                authors=[("Antonio", "Nappa"), ("X", "Y")],
            ),
        )
    ]
    cov = discover.Coverage(
        total=4, resolved=3, with_refs=3, references=30, duplicates=[["a", "a2"]]
    )
    discover.render(rows, cov, _args())
    original = capsys.readouterr().out

    monkeypatch.chdir(tmp_path)
    discover.save_last(tmp_path / discover.LAST_FILE, rows, cov, 2)
    assert discover.run_discover(_args(last=True)) == 0
    out = capsys.readouterr().out

    assert out.startswith("Last `zobs discover` run, ")
    assert out.split("\n", 1)[1] == original  # identical, block for block


def test_last_needs_no_zotero_config(tmp_path, monkeypatch, capsys) -> None:
    _install_last(tmp_path, monkeypatch)
    monkeypatch.setattr(
        discover, "load_config", lambda: pytest.fail("--last must not read .env")
    )
    assert discover.run_discover(_args(last=True)) == 0
    assert "Paper 2" in capsys.readouterr().out


def test_last_json_carries_ranks(tmp_path, monkeypatch, capsys) -> None:
    _install_last(tmp_path, monkeypatch)
    assert discover.run_discover(_args(last=True, as_json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [c["rank"] for c in payload["candidates"]] == [1, 2, 3]


def test_last_without_a_saved_run_names_the_path(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert discover.run_discover(_args(last=True)) == 1
    assert str(tmp_path / discover.LAST_FILE) in capsys.readouterr().err


def test_first_format_saved_list_still_works(tmp_path, monkeypatch, capsys) -> None:
    # regression: lists saved before --last kept only rank/key/title
    monkeypatch.chdir(tmp_path)
    path = tmp_path / discover.LAST_FILE
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([{"rank": 1, "key": "10.1/old", "title": "Old"}]))
    assert discover.run_discover(_args(last=True)) == 0
    assert "1. Old  10.1/old" in capsys.readouterr().out
    assert discover.run_discover(_args(ignore=["1"])) == 0
    assert discover.load_ignore(tmp_path / discover.IGNORE_FILE) == {"10.1/old"}
