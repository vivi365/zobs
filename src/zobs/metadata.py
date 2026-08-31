"""
Resolve canonical publication metadata for a thin Zotero item.

Order of preference: a resolvable identifier (DOI / arXiv id) first, then a
strict title + author + year match against DBLP, Crossref and OpenAlex.

Sources, all keyless:
    crossref  api.crossref.org      journals, ACM/Springer/Elsevier proceedings
    datacite  api.datacite.org      arXiv (10.48550), Zenodo, figshare DOIs
    arxiv     export.arxiv.org      preprints
    dblp      dblp.org              USENIX / NDSS / S&P / CS venues
    openalex  api.openalex.org      catch-all aggregator
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, fields

from rapidfuzz import fuzz

from zobs.cache import ResponseCache

# ── data model ────────────────────────────────────────────────────────────────


@dataclass
class Metadata:
    title: str = ""  # the matched work's title, for scoring
    container_title: str | None = None  # journal or proceedings name
    container_short: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    year: int | None = None
    month: int | None = None
    doi: str | None = None
    issn: str | None = None
    isbn: str | None = None
    publisher: str | None = None
    event_name: str | None = None
    event_place: str | None = None
    arxiv_id: str | None = None
    work_type: str | None = None  # journal | conference | preprint | book | report
    authors: list[tuple[str, str]] = field(default_factory=list)  # (given, family)
    source: str = ""

    def is_empty(self) -> bool:
        return not any(
            getattr(self, f.name)
            for f in fields(self)
            if f.name not in {"source", "work_type", "authors", "title"}
        )


@dataclass
class Resolution:
    metadata: Metadata | None
    tier: str  # exact | high | low | none
    detail: str = ""  # human-readable ("Crossref, DOI" / "DBLP 0.71")


# ── normalisation / scoring ──────────────────────────────────────────────────

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")


def _norm_title(s: str) -> str:
    return _WS.sub(" ", _PUNCT.sub(" ", (s or "").lower())).strip()


def item_authors(data: dict) -> list[tuple[str, str]]:
    out = []
    for c in data.get("creators", []):
        if c.get("creatorType") != "author":
            continue
        out.append((c.get("firstName", ""), c.get("lastName", "")))
    return out


def _first_surname(authors: list[tuple[str, str]]) -> str:
    if not authors:
        return ""
    given, family = authors[0]
    name = family or given
    return name.split()[-1].lower() if name else ""


_KEY_TERM = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]{1,14})\s*[:\-—]")


def _key_term(title: str) -> str:
    """
    The system / tool name a CS title leads with ("MOVERY:", "V1SCAN -").
    Used as a hard gate: if the item has one, a candidate that doesn't
    mention it is a different paper.
    """
    m = _KEY_TERM.match((title or "").strip())
    if not m:
        return ""
    term = m.group(1)
    return (
        term.lower()
        if (any(c.isupper() for c in term) or any(c.isdigit() for c in term))
        else ""
    )


@dataclass
class Match:
    ratio: float
    author_ok: bool
    year_ok: bool
    key_ok: bool

    @property
    def high(self) -> bool:
        return self.author_ok and self.year_ok and self.key_ok

    @property
    def plausible(self) -> bool:
        return self.author_ok and self.key_ok


def score(cand: Metadata, data: dict, have_year: int | None) -> Match:
    have_title = data.get("title", "")
    ratio = fuzz.token_sort_ratio(_norm_title(cand.title), _norm_title(have_title))
    author_ok = bool(cand.authors) and _first_surname(cand.authors) == _first_surname(
        item_authors(data)
    )
    if cand.year is None or have_year is None:
        year_ok = True
    else:
        year_ok = abs(cand.year - have_year) <= 1
    key = _key_term(have_title)
    key_ok = not key or key in _norm_title(cand.title)
    return Match(ratio, author_ok, year_ok, key_ok)


# ── DOI / arXiv id helpers ───────────────────────────────────────────────────

_ARXIV_IN_DOI = re.compile(r"10\.48550/arxiv\.(.+)", re.IGNORECASE)
_ARXIV_IN_URL = re.compile(r"arxiv\.org/abs/([^\s?]+)", re.IGNORECASE)
_ARXIV_PREFIX = re.compile(r"^arxiv:", re.IGNORECASE)


def arxiv_id_of(data: dict) -> str | None:
    for key in ("archiveID", "url", "DOI"):
        val = str(data.get(key) or "")
        if not val:
            continue
        if _ARXIV_PREFIX.match(val):
            return _ARXIV_PREFIX.sub("", val).strip()
        m = _ARXIV_IN_DOI.search(val)
        if m:
            return m.group(1).strip()
        m = _ARXIV_IN_URL.search(val)
        if m:
            return m.group(1).strip()
    return None


def is_datacite_doi(doi: str) -> bool:
    return doi.lower().startswith(("10.48550/", "10.5281/", "10.6084/"))


# ── adapters: source payload -> Metadata ─────────────────────────────────────

_CR_TYPE = {
    "journal-article": "journal",
    "proceedings-article": "conference",
    "posted-content": "preprint",
    "book": "book",
    "book-chapter": "book",
    "monograph": "book",
    "report": "report",
    "dissertation": "report",
}


def _first(x):
    if isinstance(x, list):
        return x[0] if x else None
    return x


def _titled(m: Metadata, title: str) -> Metadata:
    m.title = title or ""
    return m


def _from_crossref(msg: dict) -> Metadata:
    issued = (msg.get("issued") or {}).get("date-parts") or [[]]
    parts = issued[0] if issued and issued[0] else []
    year = parts[0] if len(parts) >= 1 else None
    month = parts[1] if len(parts) >= 2 else None
    event = msg.get("event") or {}
    m = Metadata(
        container_title=_first(msg.get("container-title")),
        container_short=_first(msg.get("short-container-title")),
        volume=msg.get("volume"),
        issue=msg.get("issue"),
        pages=msg.get("page"),
        year=year,
        month=month,
        doi=(msg.get("DOI") or "").lower() or None,
        issn=_first(msg.get("ISSN")),
        isbn=_first(msg.get("ISBN")),
        publisher=msg.get("publisher"),
        event_name=event.get("name"),
        event_place=event.get("location"),
        work_type=_CR_TYPE.get(msg.get("type", ""), None),
        authors=[
            (a.get("given", ""), a.get("family", ""))
            for a in msg.get("author", [])
            if a.get("family") or a.get("given")
        ],
        source="crossref",
    )
    return _titled(m, _first(msg.get("title")) or "")


def _from_datacite(attrs: dict) -> Metadata:
    year = attrs.get("publicationYear")
    creators = attrs.get("creators", [])
    authors = []
    for c in creators:
        given = c.get("givenName", "")
        family = c.get("familyName", "")
        if not (given or family) and c.get("name"):
            name = c["name"]
            family, _, given = name.partition(", ")
        authors.append((given, family))
    titles = attrs.get("titles") or [{}]
    m = Metadata(
        year=int(year) if year else None,
        doi=(attrs.get("doi") or "").lower() or None,
        publisher=attrs.get("publisher"),
        container_title=(attrs.get("container") or {}).get("title"),
        work_type="preprint" if is_datacite_doi(attrs.get("doi", "")) else None,
        authors=authors,
        source="datacite",
    )
    return _titled(m, titles[0].get("title", ""))


_ATOM = "{http://www.w3.org/2005/Atom}"
_ARX = "{http://arxiv.org/schemas/atom}"


def _from_arxiv(entry: ET.Element) -> Metadata:
    def text(tag: str) -> str:
        el = entry.find(tag)
        return (el.text or "").strip() if el is not None else ""

    published = text(f"{_ATOM}published")
    year = int(published[:4]) if published[:4].isdigit() else None
    month = int(published[5:7]) if published[5:7].isdigit() else None
    authors = []
    for a in entry.findall(f"{_ATOM}author"):
        name = a.find(f"{_ATOM}name")
        if name is not None and name.text:
            given, _, family = name.text.rpartition(" ")
            authors.append((given, family))
    doi = text(f"{_ARX}doi").lower() or None
    journal_ref = text(f"{_ARX}journal_ref")
    abs_id = text(f"{_ATOM}id").rsplit("/abs/", 1)[-1]
    m = Metadata(
        year=year,
        month=month,
        doi=doi,
        arxiv_id=abs_id or None,
        container_title=journal_ref or None,
        work_type="preprint" if not journal_ref else None,
        authors=authors,
        source="arxiv",
    )
    return _titled(m, text(f"{_ATOM}title"))


_DBLP_TYPE = {
    "Journal Articles": "journal",
    "Conference and Workshop Papers": "conference",
    "Informal and Other Publications": "preprint",
}


def _from_dblp(info: dict) -> Metadata:
    raw_authors = ((info.get("authors") or {}).get("author")) or []
    if isinstance(raw_authors, dict):
        raw_authors = [raw_authors]
    authors = []
    for a in raw_authors:
        text = a.get("text", "") if isinstance(a, dict) else str(a)
        text = re.sub(r"\s+\d{4}$", "", text)  # strip DBLP homonym suffix
        given, _, family = text.rpartition(" ")
        authors.append((given, family))
    year = info.get("year")
    m = Metadata(
        container_title=info.get("venue"),
        volume=info.get("volume"),
        issue=info.get("number"),
        pages=info.get("pages"),
        year=int(year) if year else None,
        doi=(info.get("doi") or "").lower() or None,
        work_type=_DBLP_TYPE.get(info.get("type", ""), None),
        authors=authors,
        source="dblp",
    )
    return _titled(m, info.get("title", "").rstrip("."))


_OA_TYPE = {
    "article": "journal",
    "proceedings-article": "conference",
    "preprint": "preprint",
    "book": "book",
    "book-chapter": "book",
    "report": "report",
}


def _from_openalex(work: dict) -> Metadata:
    biblio = work.get("biblio") or {}
    pages = None
    if biblio.get("first_page"):
        pages = biblio["first_page"]
        if biblio.get("last_page"):
            pages = f"{biblio['first_page']}-{biblio['last_page']}"
    loc = (work.get("primary_location") or {}).get("source") or {}
    authors = []
    for a in work.get("authorships", []):
        name = (a.get("author") or {}).get("display_name") or a.get(
            "raw_author_name", ""
        )
        given, _, family = name.rpartition(" ")
        authors.append((given, family))
    doi = (work.get("doi") or "").replace("https://doi.org/", "").lower() or None
    m = Metadata(
        container_title=loc.get("display_name"),
        issn=_first(loc.get("issn")),
        volume=biblio.get("volume"),
        issue=biblio.get("issue"),
        pages=pages,
        year=work.get("publication_year"),
        doi=doi,
        publisher=loc.get("host_organization_name"),
        work_type=_OA_TYPE.get(work.get("type", ""), None),
        authors=authors,
        source="openalex",
    )
    return _titled(m, work.get("title") or work.get("display_name") or "")


# ── queries ─────────────────────────────────────────────────────────────────


def _q(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def crossref_by_doi(
    doi: str, cache: ResponseCache, mailto: str | None
) -> Metadata | None:
    url = f"https://api.crossref.org/works/{_q(doi)}"
    if mailto:
        url += f"?mailto={_q(mailto)}"
    data = cache.fetch_json(url)
    if not data or "message" not in data:
        return None
    return _from_crossref(data["message"])


def crossref_search(
    title: str, cache: ResponseCache, mailto: str | None
) -> list[Metadata]:
    select = "DOI,title,author,container-title,short-container-title,volume,issue,page,issued,type,publisher,ISSN,ISBN,event"
    url = (
        "https://api.crossref.org/works?"
        f"query.bibliographic={_q(title)}&rows=5&select={select}"
    )
    if mailto:
        url += f"&mailto={_q(mailto)}"
    data = cache.fetch_json(url)
    items = ((data or {}).get("message") or {}).get("items") or []
    return [_from_crossref(m) for m in items]


def datacite_by_doi(doi: str, cache: ResponseCache) -> Metadata | None:
    url = f"https://api.datacite.org/dois/{_q(doi)}"
    data = cache.fetch_json(url)
    attrs = ((data or {}).get("data") or {}).get("attributes")
    return _from_datacite(attrs) if attrs else None


def arxiv_by_id(arxiv_id: str, cache: ResponseCache) -> Metadata | None:
    url = f"http://export.arxiv.org/api/query?id_list={_q(arxiv_id)}&max_results=1"
    body = cache.fetch_text(url, accept="application/atom+xml")
    if not body:
        return None
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    entry = root.find(f"{_ATOM}entry")
    if entry is None or entry.find(f"{_ATOM}title") is None:
        return None
    return _from_arxiv(entry)


def arxiv_search(title: str, cache: ResponseCache) -> list[Metadata]:
    url = (
        "http://export.arxiv.org/api/query?"
        f"search_query=ti:{_q(chr(34) + title + chr(34))}&max_results=5"
    )
    body = cache.fetch_text(url, accept="application/atom+xml")
    if not body:
        return []
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    return [
        _from_arxiv(e)
        for e in root.findall(f"{_ATOM}entry")
        if e.find(f"{_ATOM}title") is not None
    ]


def dblp_search(title: str, cache: ResponseCache) -> list[Metadata]:
    url = f"https://dblp.org/search/publ/api?q={_q(title)}&format=json&h=6"
    data = cache.fetch_json(url)
    hits = (((data or {}).get("result") or {}).get("hits") or {}).get("hit") or []
    return [_from_dblp(h["info"]) for h in hits if "info" in h]


def openalex_by_doi(doi: str, cache: ResponseCache) -> Metadata | None:
    url = f"https://api.openalex.org/works/doi:{_q(doi)}"
    data = cache.fetch_json(url)
    return _from_openalex(data) if data and data.get("id") else None


def openalex_search(title: str, cache: ResponseCache) -> list[Metadata]:
    url = f"https://api.openalex.org/works?filter=title.search:{_q(title)}&per_page=5"
    data = cache.fetch_json(url)
    return [_from_openalex(w) for w in (data or {}).get("results", [])]


# ── top-level resolve ───────────────────────────────────────────────────────

_SEARCHERS = {
    "dblp": lambda t, c, m: dblp_search(t, c),
    "crossref": lambda t, c, m: crossref_search(t, c, m),
    "openalex": lambda t, c, m: openalex_search(t, c),
    "arxiv": lambda t, c, m: arxiv_search(t, c),
}


def resolve(
    data: dict,
    *,
    sources: list[str],
    cache: ResponseCache,
    min_ratio: float,
    mailto: str | None = None,
) -> Resolution:
    title = data.get("title", "")
    ymatch = re.search(r"\b(?:19|20)\d{2}\b", str(data.get("date") or ""))
    have_year = int(ymatch.group(0)) if ymatch else None
    doi = str(data.get("DOI") or "").strip().lower()
    arxiv_id = arxiv_id_of(data)

    # 1. resolvable identifier -> exact
    if arxiv_id and "arxiv" in sources:
        md = arxiv_by_id(arxiv_id, cache)
        if md and not md.is_empty():
            md.arxiv_id = md.arxiv_id or arxiv_id
            return Resolution(md, "exact", f"arXiv:{arxiv_id}")
    if doi:
        if is_datacite_doi(doi) and "datacite" in sources:
            md = datacite_by_doi(doi, cache)
            if md and not md.is_empty():
                return Resolution(md, "exact", "DataCite DOI")
        if "crossref" in sources and not is_datacite_doi(doi):
            md = crossref_by_doi(doi, cache, mailto)
            if md and not md.is_empty():
                return Resolution(md, "exact", "Crossref DOI")
        if "openalex" in sources:
            md = openalex_by_doi(doi, cache)
            if md and not md.is_empty():
                return Resolution(md, "exact", "OpenAlex DOI")

    # 2. title search across the configured sources
    best: tuple[float, Metadata] | None = None
    for name in sources:
        searcher = _SEARCHERS.get(name)
        if not searcher:
            continue
        for cand in searcher(title, cache, mailto):
            if cand.is_empty():
                continue
            m = score(cand, data, have_year)
            if m.ratio >= min_ratio and m.high:
                return Resolution(cand, "high", f"{cand.source} {m.ratio:.0f}")
            if m.ratio >= 80 and m.plausible and (best is None or m.ratio > best[0]):
                best = (m.ratio, cand)

    if best is not None:
        return Resolution(best[1], "low", f"{best[1].source} {best[0]:.0f}")
    return Resolution(None, "none", "")
