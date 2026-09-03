"""
``zobs discover`` — backward snowballing over the reference lists of the papers
already in the selection.

Every paper in the selection has a bibliography. A paper that turns up in many
of those bibliographies but is not in the collection is very likely something
the user should have read. This finds those, ranks them, and names which of the
user's own papers cite each one.

Read-only: nothing is written to Zotero or to refs.bib. The one file it touches
is the ignore list, and only when asked.

See specs/discover-spec.md for the design.
"""

from __future__ import annotations

import json
import math
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from pyzotero import errors as zotero_errors, zotero

from zobs import metadata as md
from zobs.augment import collect
from zobs.cache import ResponseCache
from zobs.sync import load_config, year_from_date

# augment's cache directory, shared deliberately. The cache is keyed by request
# URL, so every OpenAlex record `zobs augment` has already fetched is reused
# here at no cost, and vice versa.
CACHE_DIR = Path("references") / ".zobs-cache" / "augment"

# Unlike the cache, this one is meant to be committed: a rejected suggestion
# has to stay rejected across machines and across runs.
IGNORE_FILE = Path("references") / ".zobs-ignore"

_IGNORE_HEADER = (
    "# zobs discover: suggestions to never show again.\n"
    "# One OpenAlex work id (W123...) or DOI per line. '#' starts a comment.\n"
)

# Title matches are held to augment's default bar. There is no flag to lower it:
# a wrong paper's reference list does not just produce one bad row, it feeds
# dozens of wrong citations into the ranking.
MIN_RATIO = 93.0

# Ceiling on how many candidates get a metadata lookup, so a very large
# selection cannot turn into a hundred OpenAlex requests. Applied after sorting
# by raw count, so only the thinnest evidence is dropped.
MAX_LOOKUPS = 400


# ── the collection side ─────────────────────────────────────────────────────


@dataclass
class Held:
    """A paper already in the selection, and its OpenAlex resolution."""

    citekey: str
    key: str
    title: str
    doi: str
    openalex_id: str | None = None
    referenced_works: list[str] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return bool(self.openalex_id)

    @property
    def has_refs(self) -> bool:
        return bool(self.referenced_works)


def resolve_work(data: dict, cache, min_ratio: float = MIN_RATIO) -> md.Metadata | None:
    """
    Resolve one selection item to an OpenAlex work.

    Deliberately not ``metadata.resolve``: that returns the first good hit from
    any source, and a DBLP or Crossref hit carries no OpenAlex id, hence no
    reference list. A DOI is one exact request whose response already contains
    ``referenced_works``, so the bibliography costs nothing extra. Without a
    DOI, fall back to a title search and accept only the high tier; the low
    tier is never good enough here.
    """
    doi = str(data.get("DOI") or "").strip().lower()
    if doi:
        cand = md.openalex_by_doi(doi, cache)
        if cand and cand.openalex_id:
            return cand

    title = str(data.get("title") or "").strip()
    if not title:
        return None
    year = year_from_date(data.get("date"))
    have_year = int(year) if year else None
    for cand in md.openalex_search(title, cache):
        if not cand.openalex_id:
            continue
        m = md.score(cand, data, have_year)
        if m.ratio >= min_ratio and m.high:
            return cand
    return None


def resolve_selection(cands, cache, *, quiet: bool = False) -> list[Held]:
    held = []
    for c in cands:
        h = Held(citekey=c.citekey, key=c.key, title=c.title, doi=_norm_doi(c.doi))
        try:
            work = resolve_work(c.item["data"], cache)
        except Exception as e:  # noqa: BLE001 - one bad lookup must not abort
            if not quiet:
                print(f"  [warn] {c.citekey}: lookup failed ({e})", file=sys.stderr)
            work = None
        if work:
            h.openalex_id = work.openalex_id
            h.referenced_works = work.referenced_works
            h.doi = h.doi or _norm_doi(work.doi)
        held.append(h)
    return held


def build_index(held: list[Held]) -> dict[str, set[str]]:
    """``openalex_id -> citekeys in the collection that reference it``.

    The citekeys are the point. A bare count is a recommendation; a count plus
    "cited by these four of your papers" is something the user can check.
    """
    index: dict[str, set[str]] = {}
    for h in held:
        for ref in h.referenced_works:
            index.setdefault(ref, set()).add(h.citekey)
    return index


# ── ranking ─────────────────────────────────────────────────────────────────


def rank_score(count: int, cited_by_count: int | None) -> float:
    """
    How concentrated a paper's pull is inside this collection.

    Raw count ranks famous papers first, and the famous papers are exactly the
    ones the user has already decided not to hold. Dividing by the log of the
    global citation count asks a better question: is this paper specific to what
    the user is reading, or does everyone cite it?

        score = count / log10(10 + cited_by_count)

    The divisor runs from 1.0 (uncited or unknown, no penalty) to about 4.6 at
    40,000 citations, so the global count reorders papers without ever
    overwhelming the local evidence. Cited by 5 of 60 with 200 citations
    globally scores 2.15; cited by the same 5 with 40,000 scores 1.09.

    Swap this one function to change the ranking.
    """
    return count / math.log10(10 + max(cited_by_count or 0, 0))


@dataclass
class Discovery:
    openalex_id: str
    citekeys: list[str]  # sorted; the evidence
    metadata: md.Metadata

    @property
    def count(self) -> int:
        return len(self.citekeys)

    @property
    def cited_by_count(self) -> int:
        return self.metadata.cited_by_count or 0

    @property
    def score(self) -> float:
        return rank_score(self.count, self.cited_by_count)

    @property
    def doi(self) -> str:
        return _norm_doi(self.metadata.doi)

    @property
    def first_author(self) -> str:
        if not self.metadata.authors:
            return ""
        given, family = self.metadata.authors[0]
        return family or given

    def sort_key(self) -> tuple:
        # score first, then the raw local count, then prefer the less-cited of
        # two otherwise equal papers, then the title so runs are reproducible
        return (-self.score, -self.count, self.cited_by_count, self.metadata.title)


def rank(
    index: dict[str, set[str]],
    cache,
    *,
    held_ids: set[str],
    held_dois: set[str],
    ignore: set[str],
    min_count: int,
    limit: int,
) -> list[Discovery]:
    """Filter, fetch metadata in batches, weight, and return the top `limit`."""
    held_lower = {i.lower() for i in held_ids}
    pool = [
        (oid, cks)
        for oid, cks in index.items()
        if len(cks) >= min_count
        and oid.lower() not in held_lower
        and oid.lower() not in ignore
    ]
    # Sort by raw count only to decide who gets a lookup. The real ranking
    # needs cited_by_count, which is what the lookup is for, so the pool has to
    # be everything at or above --min-count rather than the top --limit by
    # count: truncating on raw count first would throw away precisely the niche
    # papers the weighting exists to surface.
    pool.sort(key=lambda p: (-len(p[1]), p[0]))
    pool = pool[:MAX_LOOKUPS]

    records = {
        m.openalex_id: m
        for m in md.openalex_by_ids([oid for oid, _ in pool], cache)
        if m.openalex_id
    }

    out = []
    for oid, cks in pool:
        m = records.get(oid)
        if m is None:  # merged or withdrawn id, nothing to show
            continue
        doi = _norm_doi(m.doi)
        if doi and (doi in held_dois or doi in ignore):
            continue
        out.append(Discovery(oid, sorted(cks), m))
    out.sort(key=Discovery.sort_key)
    return out[:limit]


# ── ignore list ─────────────────────────────────────────────────────────────

_KEY_PREFIXES = (
    "https://openalex.org/",
    "http://openalex.org/",
    "openalex.org/",
    "https://doi.org/",
    "http://doi.org/",
    "doi.org/",
    "doi:",
)


def normalise_key(raw: str) -> str:
    """An OpenAlex id or DOI in any of its usual dresses -> one comparable form."""
    key = str(raw or "").strip().lower()
    for prefix in _KEY_PREFIXES:
        if key.startswith(prefix):
            key = key[len(prefix) :]
            break
    return key.strip()


def _norm_doi(raw: str | None) -> str:
    key = normalise_key(raw or "")
    return key if key.startswith("10.") else ""


def load_ignore(path: Path) -> set[str]:
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        # only a whitespace-preceded '#' opens a comment, since a DOI may
        # legally contain one
        key = normalise_key(line.split(" #", 1)[0])
        if key:
            out.add(key)
    return out


def append_ignore(path: Path, keys: list[str]) -> list[str]:
    """Add keys to the ignore file, skipping ones already there. Returns the new."""
    existing = load_ignore(path)
    added: list[str] = []
    for raw in keys:
        key = normalise_key(raw)
        if key and key not in existing:
            existing.add(key)
            added.append(key)
    if not added:
        return []
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "" if path.exists() else _IGNORE_HEADER
    with path.open("a", encoding="utf-8") as fh:
        fh.write(header + "".join(f"{k}\n" for k in added))
    return added


# ── output ──────────────────────────────────────────────────────────────────


@dataclass
class Coverage:
    total: int
    resolved: int
    with_refs: int
    references: int

    def line(self) -> str:
        return (
            f"Resolved {self.resolved}/{self.total} selection items; "
            f"{self.with_refs} had reference data "
            f"({self.references} references read)."
        )


def _clip(s: str, width: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= width else s[: width - 1] + "…"


def _via(citekeys: list[str], budget: int) -> str:
    shown: list[str] = []
    for ck in citekeys:
        if shown and len(", ".join(shown)) + len(ck) + 2 > budget:
            break
        shown.append(ck)
    rest = len(citekeys) - len(shown)
    return ", ".join(shown) + (f" (+{rest} more)" if rest else "")


def render(rows: list[Discovery], cov: Coverage, args) -> None:
    print(cov.line())
    if not rows:
        print(
            f"\nNothing cited by {args.min_count} or more of them that you do not "
            "already hold."
        )
        if cov.with_refs < cov.total:
            print(
                "OpenAlex reference coverage is patchy for some venues (USENIX and "
                "NDSS especially), so this may say more about the data than about "
                "your reading."
            )
        return

    print(
        f"\n{len(rows)} paper{'' if len(rows) == 1 else 's'} cited by at least "
        f"{args.min_count} of your papers but not in your collection:\n"
    )
    width = max(shutil.get_terminal_size((110, 24)).columns, 90)
    title_w = width - 62
    print(
        f"  {'#':>2}  {'cited':>5}  {'global':>7}  {'year':>4}  "
        f"{'first author':<16}  {'venue':<20}  title"
    )
    for i, d in enumerate(rows, 1):
        m = d.metadata
        print(
            f"  {i:>2}  {d.count:>5}  {d.cited_by_count:>7,}  "
            f"{(m.year or ''):>4}  {_clip(d.first_author, 16):<16}  "
            f"{_clip(m.container_title or '', 20):<20}  {_clip(m.title, title_w)}"
        )
        print(f"      via {_via(d.citekeys, width - 60)}")
        print(f"      {('doi ' + d.doi) if d.doi else 'openalex ' + d.openalex_id}")

    print(
        f"\nNot interested in one? `zobs discover --ignore "
        f"{rows[0].doi or rows[0].openalex_id}` keeps it out of future runs."
    )


def as_json(rows: list[Discovery], cov: Coverage) -> str:
    return json.dumps(
        {
            "coverage": {
                "selected": cov.total,
                "resolved": cov.resolved,
                "withReferences": cov.with_refs,
                "referencesRead": cov.references,
            },
            "candidates": [
                {
                    "openalexID": d.openalex_id,
                    "title": d.metadata.title,
                    "firstAuthor": d.first_author or None,
                    "year": d.metadata.year,
                    "venue": d.metadata.container_title,
                    "doi": d.doi or None,
                    "citedByHeld": d.count,
                    "citedByGlobal": d.cited_by_count,
                    "score": round(d.score, 4),
                    "via": d.citekeys,
                }
                for d in rows
            ],
        },
        indent=2,
    )


# ── entry point ─────────────────────────────────────────────────────────────


def run_discover(args) -> int:
    ignore_path = Path.cwd() / IGNORE_FILE

    if args.ignore:
        added = append_ignore(ignore_path, args.ignore)
        skipped = len(args.ignore) - len(added)
        print(
            f"{len(added)} added to {IGNORE_FILE}"
            + (f", {skipped} already there" if skipped else "")
        )
        for key in added:
            print(f"  {key}")
        return 0

    cfg = load_config()
    zot = zotero.Zotero(cfg["user_id"], "user", cfg["api_key"])
    try:
        cands = collect(cfg, zot)
    except (ValueError, RuntimeError, zotero_errors.HTTPError) as e:
        print(f"[error] {e}", file=sys.stderr)
        return 1
    if not cands:
        print("Nothing in the current selection.")
        return 0

    cache = ResponseCache(Path.cwd() / CACHE_DIR, refresh=args.refresh)
    if not args.as_json:
        print(f"Reading the reference lists of {len(cands)} papers...")

    held = resolve_selection(cands, cache, quiet=args.as_json)
    index = build_index(held)
    cov = Coverage(
        total=len(held),
        resolved=sum(1 for h in held if h.resolved),
        with_refs=sum(1 for h in held if h.has_refs),
        references=sum(len(h.referenced_works) for h in held),
    )

    rows = rank(
        index,
        cache,
        held_ids={h.openalex_id for h in held if h.openalex_id},
        held_dois={h.doi for h in held if h.doi},
        ignore=load_ignore(ignore_path),
        min_count=args.min_count,
        limit=args.limit,
    )

    if args.as_json:
        print(as_json(rows, cov))
    else:
        render(rows, cov, args)
    return 0
