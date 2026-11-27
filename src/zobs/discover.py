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
import re
import shutil
import sys
import textwrap
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

# The list the last run printed, so `--ignore 1-25` means exactly the rows the
# user just saw. Machine-local, so it lives next to the (gitignored) cache.
LAST_FILE = Path("references") / ".zobs-cache" / "discover-last.json"

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


def dedupe(held: list[Held]) -> tuple[list[Held], list[list[str]]]:
    """
    One entry per OpenAlex work, so a paper that sits in Zotero twice
    ("liStudy2017" and "liStudy2017a") counts once toward everything it cites.

    Returns the kept papers, in their original order, and the duplicate groups
    as citekey lists, kept citekey first. The kept one is the alphabetically
    first citekey, which is the unsuffixed one when Better BibTeX added "a".
    Unresolved papers have no reference list and are kept as they are.
    """
    groups: dict[str, list[Held]] = {}
    for h in held:
        if h.openalex_id:
            groups.setdefault(h.openalex_id.lower(), []).append(h)
    keep = {id(min(g, key=lambda h: h.citekey)) for g in groups.values()}
    kept = [h for h in held if not h.openalex_id or id(h) in keep]
    dups = []
    for g in groups.values():
        if len(g) > 1:
            first = min(g, key=lambda h: h.citekey)
            dups.append(
                [first.citekey] + sorted(h.citekey for h in g if h is not first)
            )
    return kept, sorted(dups)


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


# ── ignoring by rank ────────────────────────────────────────────────────────

_RANKS = re.compile(r"^\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*$")


def is_rank_spec(raw: str) -> bool:
    """'3', '1-25', '1,4,7-9'. A DOI always has a '/', an OpenAlex id a 'W'."""
    return bool(_RANKS.match(str(raw).replace(" ", "")))


def parse_ranks(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.replace(" ", "").split(","):
        lo, _, hi = part.partition("-")
        a, b = int(lo), int(hi or lo)
        if a > b:
            a, b = b, a
        out.extend(n for n in range(a, b + 1) if n not in out)
    return out


def save_last(path: Path, rows: list[Discovery]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                [
                    {
                        "rank": i,
                        "key": d.doi or d.openalex_id,
                        "title": d.metadata.title,
                    }
                    for i, d in enumerate(rows, 1)
                ],
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass  # numbering is a convenience; DOIs and ids still work


def load_last(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def expand_ignore(raws: list[str], last: list[dict]) -> list[tuple[str, str]]:
    """
    ``--ignore`` arguments -> ``(key, title)`` pairs. Rank specs refer to the
    list the last run printed; anything else is a DOI, OpenAlex id or link and
    passes through with no title. Raises ValueError on a rank that run did not
    show, so a typo never silently ignores the wrong paper.
    """
    by_rank = {int(r["rank"]): r for r in last if "rank" in r and "key" in r}
    out: list[tuple[str, str]] = []
    for raw in raws:
        if not is_rank_spec(raw):
            out.append((raw, ""))
            continue
        if not by_rank:
            raise ValueError(
                "no saved list to number from; run `zobs discover` first, "
                "then `--ignore 1-25` refers to the rows it printed"
            )
        ranks = parse_ranks(raw)
        missing = [n for n in ranks if n not in by_rank]
        if missing:
            raise ValueError(
                f"the last run showed rows 1-{max(by_rank)}; "
                f"no row {', '.join(map(str, missing))}"
            )
        out.extend((by_rank[n]["key"], by_rank[n].get("title", "")) for n in ranks)
    return out


# ── output ──────────────────────────────────────────────────────────────────


@dataclass
class Coverage:
    total: int
    resolved: int
    with_refs: int
    references: int
    duplicates: list[list[str]] = field(default_factory=list)

    def line(self) -> str:
        return (
            f"Resolved {self.resolved}/{self.total} selection items; "
            f"{self.with_refs} had reference data "
            f"({self.references} references read)."
        )


_INDENT = " " * 6


def _wrap(text: str, width: int, first: str, rest: str | None = None) -> str:
    lines = textwrap.wrap(
        " ".join((text or "").split()),
        width=width,
        initial_indent=first,
        subsequent_indent=first if rest is None else rest,
        break_on_hyphens=False,
        break_long_words=False,
    )
    return "\n".join(lines) or first.rstrip()


def _byline(d: Discovery) -> str:
    """'Nappa et al. 2015 · IEEE Symposium on ...': who, when, where."""
    m = d.metadata
    who = d.first_author or "unknown author"
    if len(m.authors) > 1:
        who += " et al."
    parts = [f"{who} {m.year}" if m.year else who]
    if m.container_title:
        parts.append(" ".join(m.container_title.split()))
    return " · ".join(parts)


def _url(d: Discovery) -> str:
    """A link the terminal can open: the DOI resolver, else the OpenAlex page."""
    if d.doi:
        return f"https://doi.org/{d.doi}"
    return f"https://openalex.org/{d.openalex_id}"


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

    if cov.duplicates:
        n = len(cov.duplicates)
        print(
            f"{n} paper{'' if n == 1 else 's'} {'is' if n == 1 else 'are'} in your "
            "selection more than once; each is counted once:"
        )
        for group in cov.duplicates:
            print(f"  {' = '.join(group)}")
    print(
        f"\n{len(rows)} paper{'' if len(rows) == 1 else 's'} cited by at least "
        f"{args.min_count} of your papers but not in your collection:\n"
    )
    # One block per suggestion. The suggested paper comes first with its full
    # title and a link; the citekeys underneath are the user's OWN papers that
    # cite it, labelled as such so they are not read as the suggestion.
    width = min(max(shutil.get_terminal_size((100, 24)).columns, 60), 110)
    for i, d in enumerate(rows, 1):
        print(_wrap(d.metadata.title or "(untitled)", width, f"  {i:>2}. ", _INDENT))
        print(_wrap(_byline(d), width, _INDENT))
        print(f"{_INDENT}{_url(d)}")
        yours = "1 of your papers" if d.count == 1 else f"{d.count} of your papers"
        print(f"{_INDENT}cited by {yours} ({d.cited_by_count:,} globally):")
        print(_wrap(", ".join(d.citekeys), width, _INDENT + "  "))
        print()

    print(
        "Not interested? `zobs discover --ignore 3` (or `1-10`, `2,5,7`) keeps "
        "those rows out of future runs; a DOI or link works too."
    )


def as_json(rows: list[Discovery], cov: Coverage) -> str:
    return json.dumps(
        {
            "coverage": {
                "selected": cov.total,
                "resolved": cov.resolved,
                "withReferences": cov.with_refs,
                "referencesRead": cov.references,
                "duplicates": cov.duplicates,
            },
            "candidates": [
                {
                    "openalexID": d.openalex_id,
                    "title": d.metadata.title,
                    "firstAuthor": d.first_author or None,
                    "year": d.metadata.year,
                    "venue": d.metadata.container_title,
                    "doi": d.doi or None,
                    "url": _url(d),
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
        try:
            picked = expand_ignore(args.ignore, load_last(Path.cwd() / LAST_FILE))
        except ValueError as e:
            print(f"[error] {e}", file=sys.stderr)
            return 1
        titles = {normalise_key(k): t for k, t in picked}
        added = append_ignore(ignore_path, [k for k, _ in picked])
        skipped = len(picked) - len(added)
        print(
            f"{len(added)} added to {IGNORE_FILE}"
            + (f", {skipped} already there" if skipped else "")
        )
        for key in added:
            title = titles.get(key, "")
            print(f"  {key}" + (f"  {title}" if title else ""))
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
    unique, duplicates = dedupe(held)
    index = build_index(unique)
    cov = Coverage(
        total=len(held),
        resolved=sum(1 for h in held if h.resolved),
        with_refs=sum(1 for h in held if h.has_refs),
        references=sum(len(h.referenced_works) for h in unique),
        duplicates=duplicates,
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

    save_last(Path.cwd() / LAST_FILE, rows)
    if args.as_json:
        print(as_json(rows, cov))
    else:
        render(rows, cov, args)
    return 0
