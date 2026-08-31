from __future__ import annotations

from zobs import metadata as md


def test_from_crossref_maps_fields() -> None:
    msg = {
        "DOI": "10.1145/3729378",
        "title": ["VulPA: Detecting Semantically Recurring Vulnerabilities"],
        "container-title": ["Proceedings of the ACM on Software Engineering"],
        "short-container-title": ["Proc. ACM Softw. Eng."],
        "volume": "2",
        "issue": "FSE",
        "page": "2430-2453",
        "issued": {"date-parts": [[2025, 6, 19]]},
        "publisher": "ACM",
        "ISSN": ["2994-970X"],
        "type": "journal-article",
        "author": [{"given": "Liqing", "family": "Cao"}],
    }
    m = md._from_crossref(msg)
    assert m.container_title == "Proceedings of the ACM on Software Engineering"
    assert m.container_short == "Proc. ACM Softw. Eng."
    assert (m.volume, m.issue, m.pages) == ("2", "FSE", "2430-2453")
    assert (m.year, m.month) == (2025, 6)
    assert m.work_type == "journal"
    assert m.authors == [("Liqing", "Cao")]


def test_from_dblp_conference() -> None:
    info = {
        "title": "MVP: Detecting Vulnerabilities using Patch-Enhanced Signatures.",
        "venue": "USENIX Security Symposium",
        "pages": "1165-1182",
        "year": "2020",
        "type": "Conference and Workshop Papers",
        "authors": {"author": [{"text": "Yang Xiao 0001"}, {"text": "Bihuan Chen"}]},
    }
    m = md._from_dblp(info)
    assert m.container_title == "USENIX Security Symposium"
    assert m.pages == "1165-1182"
    assert m.year == 2020
    assert m.work_type == "conference"
    assert m.authors[0] == ("Yang", "Xiao")  # homonym suffix stripped


def test_from_openalex_biblio_pages() -> None:
    work = {
        "id": "https://openalex.org/W123",
        "title": "A Vulnerability Taxonomy Methodology",
        "publication_year": 2005,
        "biblio": {"volume": "3", "issue": "1", "first_page": "49", "last_page": "62"},
        "primary_location": {"source": {"display_name": "WISA", "issn": ["1234-5678"]}},
        "type": "article",
        "authors": [],
        "authorships": [{"author": {"display_name": "Marco Vandenberghe"}}],
    }
    m = md._from_openalex(work)
    assert m.pages == "49-62"
    assert m.container_title == "WISA"
    assert m.authors == [("Marco", "Vandenberghe")]


def test_arxiv_id_of_variants() -> None:
    assert md.arxiv_id_of({"archiveID": "arXiv:2607.12316"}) == "2607.12316"
    assert md.arxiv_id_of({"DOI": "10.48550/arXiv.2401.01234"}) == "2401.01234"
    assert (
        md.arxiv_id_of({"url": "https://arxiv.org/abs/2401.09999v2"}) == "2401.09999v2"
    )
    assert md.arxiv_id_of({"DOI": "10.1145/3729378"}) is None


def test_is_datacite_doi() -> None:
    assert md.is_datacite_doi("10.48550/arXiv.2607.12316")
    assert md.is_datacite_doi("10.5281/zenodo.123")
    assert not md.is_datacite_doi("10.1145/3729378")


def test_score_matches_on_title_author_year() -> None:
    cand = md.Metadata(
        title="MVP: Detecting Vulnerabilities using Patch-Enhanced Vulnerability Signatures",
        year=2020,
        authors=[("Yang", "Xiao")],
    )
    data = {
        "title": "MVP: Detecting Vulnerabilities using Patch-Enhanced Vulnerability Signatures",
        "date": "2020",
        "creators": [
            {"creatorType": "author", "firstName": "Yang", "lastName": "Xiao"}
        ],
    }
    m = md.score(cand, data, 2020)
    assert m.ratio >= 95 and m.author_ok and m.year_ok and m.key_ok
    assert m.high


def test_score_key_term_gate_rejects_wrong_system() -> None:
    # MOVERY item vs a VUDDY candidate: overlapping words, same first author
    # surname, but the "MOVERY" key term is absent from the candidate title.
    cand = md.Metadata(
        title="VUDDY: A Scalable Approach for Vulnerable Code Clone Discovery",
        year=2017,
        authors=[("Seunghoon", "Woo")],
    )
    data = {
        "title": "MOVERY: A Precise Approach for Modified Vulnerable Code Clone Detection",
        "date": "",
        "creators": [
            {"creatorType": "author", "firstName": "Seunghoon", "lastName": "Woo"}
        ],
    }
    m = md.score(cand, data, None)
    assert not m.key_ok and not m.high and not m.plausible


class FakeCache:
    """Serves canned JSON keyed by a substring of the request URL."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.refresh = False

    def _match(self, url: str):
        for frag, payload in self.routes.items():
            if frag in url:
                return payload
        return None

    def fetch_json(self, url, **kw):
        return self._match(url)

    def fetch_text(self, url, **kw):
        return self._match(url)


def test_resolve_prefers_crossref_doi() -> None:
    cache = FakeCache(
        {
            "api.crossref.org/works/10.1145": {
                "message": {
                    "DOI": "10.1145/3729378",
                    "title": ["VulPA"],
                    "container-title": ["Proc. ACM Softw. Eng."],
                    "volume": "2",
                    "page": "2430-2453",
                    "issued": {"date-parts": [[2025, 6]]},
                    "type": "journal-article",
                    "author": [{"given": "Liqing", "family": "Cao"}],
                }
            }
        }
    )
    res = md.resolve(
        {"title": "VulPA", "DOI": "10.1145/3729378", "date": ""},
        sources=["crossref", "dblp"],
        cache=cache,
        min_ratio=93,
    )
    assert res.tier == "exact"
    assert res.metadata.volume == "2"


def test_resolve_no_match_returns_none() -> None:
    cache = FakeCache({})
    res = md.resolve(
        {"title": "Totally Unindexed Blog Post", "date": "", "DOI": ""},
        sources=["crossref", "dblp", "openalex"],
        cache=cache,
        min_ratio=93,
    )
    assert res.tier == "none" and res.metadata is None
