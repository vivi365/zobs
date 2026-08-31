"""
``zobs augment`` — detect incomplete refs.bib entries, resolve canonical
publication metadata from bibliographic APIs, fill the gaps in refs.bib and
(when the API key allows) push the same data back to Zotero.

See specs/augment-spec.md for the design.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from pyzotero import errors as zotero_errors, zotero

from zobs import metadata as md
from zobs.cache import ResponseCache
from zobs.metadata import Resolution
from zobs.sync import (
    ITEM_TYPES,
    arxiv_id_from_item,
    build_bib_entry,
    citation_key_from_item,
    load_config,
    scan_obsidian_notes,
    year_from_date,
)

# ── completeness model (IEEE-oriented, per Zotero item type) ─────────────────

REQUIRED: dict[str, list[str]] = {
    # volume/issue are filled when found but not demanded — too many
    # conference papers are misfiled as journalArticle to require them
    "journalArticle": ["creators", "title", "venue", "year", "pages"],
    "conferencePaper": ["creators", "title", "venue", "year", "pages"],
    "preprint": ["creators", "title", "year", "arxivID"],
    "book": ["creators", "title", "year", "publisher"],
    "report": ["creators", "title", "year"],
    "webpage": ["title", "year", "url"],
}
_DEFAULT_REQUIRED = ["creators", "title", "year"]

_VENUE_FIELDS = ("publicationTitle", "proceedingsTitle", "conferenceName")
_MALFORMED_PAGES = {"n. pag.", "-", "–", "—", "none", "in press"}


def _field_value(data: dict, name: str) -> str:
    if name == "year":
        return year_from_date(data.get("date"))
    if name == "creators":
        return "y" if md.item_authors(data) else ""
    if name == "venue":
        return next((str(data[f]) for f in _VENUE_FIELDS if data.get(f)), "")
    if name == "arxivID":
        return arxiv_id_from_item(data) or str(data.get("DOI") or "")
    return str(data.get(name) or "")


def missing_fields(data: dict) -> list[str]:
    req = REQUIRED.get(data.get("itemType", ""), _DEFAULT_REQUIRED)
    return [f for f in req if not _field_value(data, f)]


def malformed_fields(data: dict) -> list[str]:
    out = []
    date = str(data.get("date") or "")
    if date and not year_from_date(date):
        out.append("date")
    pages = str(data.get("pages") or "").strip().lower()
    if pages and pages in _MALFORMED_PAGES:
        out.append("pages")
    return out


@dataclass
class Candidate:
    key: str
    citekey: str
    item_type: str
    title: str
    doi: str
    missing: list[str]
    malformed: list[str]
    item: dict = field(repr=False, default_factory=dict)

    @property
    def complete(self) -> bool:
        return not self.missing and not self.malformed


def collect(cfg: dict, zot) -> list[Candidate]:
    items, _ = cfg["selector"].fetch_items(zot, ITEM_TYPES)
    obsidian = {}
    if cfg["obsidian"]:
        obsidian = scan_obsidian_notes(cfg["obsidian"])
    out = []
    for it in items:
        data = it["data"]
        key = data.get("key", "")
        _, note_ck = obsidian.get(key, (None, None))
        out.append(
            Candidate(
                key=key,
                citekey=note_ck or citation_key_from_item(data, key),
                item_type=data.get("itemType", ""),
                title=data.get("title", ""),
                doi=str(data.get("DOI") or ""),
                missing=missing_fields(data),
                malformed=malformed_fields(data),
                item=it,
            )
        )
    return out


# ── selection ───────────────────────────────────────────────────────────────


def _label(c: Candidate) -> str:
    bits = []
    if c.malformed:
        bits.append("malformed: " + ", ".join(c.malformed))
    if c.missing:
        bits.append("missing: " + ", ".join(c.missing))
    return f"{c.citekey[:44]:44}  {'  '.join(bits) or 'complete'}"


def select(cands: list[Candidate], args) -> list[Candidate]:
    if args.select:
        wanted = {s.strip() for s in args.select.split(",") if s.strip()}
        chosen = [c for c in cands if wanted & {c.key, c.citekey}]
        for unknown in wanted - {c.key for c in chosen} - {c.citekey for c in chosen}:
            print(f"[warn] not in the current selection: {unknown}", file=sys.stderr)
        return chosen

    incomplete = [c for c in cands if not c.complete]
    if args.all:
        return cands
    pool = cands if args.include_complete else incomplete
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return incomplete
    return _checklist(pool, incomplete)


def _checklist(pool: list[Candidate], preselected: list[Candidate]) -> list[Candidate]:
    if not pool:
        return []
    import questionary

    pre = {c.key for c in preselected}
    choices = [
        questionary.Choice(title=_label(c), value=c, checked=c.key in pre) for c in pool
    ]
    answer = questionary.checkbox("Entries to augment:", choices=choices).ask()
    return answer or []


# ── metadata -> Zotero field mapping ────────────────────────────────────────

_ZMAP: dict[str, dict[str, str]] = {
    "journalArticle": {
        "container_title": "publicationTitle",
        "container_short": "journalAbbreviation",
        "volume": "volume",
        "issue": "issue",
        "pages": "pages",
        "doi": "DOI",
        "issn": "ISSN",
        "publisher": "publisher",
    },
    "conferencePaper": {
        "container_title": "proceedingsTitle",
        "event_name": "conferenceName",
        "event_place": "place",
        "volume": "volume",
        "pages": "pages",
        "doi": "DOI",
        "isbn": "ISBN",
        "publisher": "publisher",
    },
    "preprint": {"doi": "DOI"},
    "book": {"publisher": "publisher", "isbn": "ISBN", "doi": "DOI"},
    "report": {"publisher": "publisher", "doi": "DOI"},
    "webpage": {"doi": "DOI"},
}


@dataclass
class Plan:
    candidate: Candidate
    resolution: Resolution
    changes: dict[str, tuple[str, str]]  # zotero field -> (old, new)

    @property
    def applied_item(self) -> dict:
        item = json.loads(json.dumps(self.candidate.item))
        for f, (_, new) in self.changes.items():
            item["data"][f] = new
        return item

    @property
    def still_missing(self) -> list[str]:
        return missing_fields(self.applied_item["data"])


def _fmt_date(year: int | None, month: int | None) -> str:
    if not year:
        return ""
    return f"{year:04d}-{month:02d}" if month else f"{year:04d}"


def plan_changes(cand: Candidate, res: Resolution, args) -> Plan:
    data = cand.item["data"]
    changes: dict[str, tuple[str, str]] = {}
    m = res.metadata
    if m is None:
        return Plan(cand, res, changes)

    repairable = set() if args.no_repair else set(cand.malformed)
    force = args.overwrite and res.tier == "exact"

    def want(zfield: str, new: str) -> None:
        new = str(new).strip()
        if not new:
            return
        cur = str(data.get(zfield) or "")
        if cur == new:
            return
        if cur and zfield not in repairable and not force:
            return
        changes[zfield] = (cur, new)

    cur_date = str(data.get("date") or "")
    if m.year and (not year_from_date(cur_date) or force):
        want("date", _fmt_date(m.year, m.month))

    for mfield, zfield in _ZMAP.get(cand.item_type, {"doi": "DOI"}).items():
        want(zfield, getattr(m, mfield) or "")

    if cand.item_type == "preprint" and m.arxiv_id:
        want("archiveID", f"arXiv:{m.arxiv_id}")

    return Plan(cand, res, changes)


# ── output ──────────────────────────────────────────────────────────────────


def show_plans(plans: list[Plan]) -> None:
    for p in plans:
        c, r = p.candidate, p.resolution
        if r.metadata is None:
            best = f" (best: {r.detail})" if r.detail else ""
            print(f"\n  {c.citekey}\n    no confident match{best} — skipped")
        elif not p.changes:
            print(f"\n  {c.citekey}  [{r.detail}] — nothing new to add")
        else:
            print(f"\n  {c.citekey}  [{r.detail}]")
            for f, (old, new) in p.changes.items():
                print(f"    {f:18} {old or '(empty)'}  ->  {new}")


# ── apply: refs.bib splice + Zotero push ────────────────────────────────────


def _splice_bib(text: str, citekey: str, entry: str, marker: str | None) -> str:
    block = re.compile(
        r"(?:^%[^\n]*\n)?^@\w+\{" + re.escape(citekey) + r",.*?^\}\n",
        re.MULTILINE | re.DOTALL,
    )
    replacement = (f"% {marker}\n" if marker else "") + entry
    if block.search(text):
        return block.sub(lambda _: replacement, text, count=1)
    sep = "" if text.endswith("\n\n") or not text else "\n"
    return text + sep + replacement


def _marker(plan: Plan) -> str | None:
    gaps = plan.still_missing
    if not gaps:
        return None
    listed = ", ".join(sorted(set(gaps)))
    if plan.resolution.metadata is None:
        return f"zobs: no confident match — still missing {{{listed}}}"
    return f"zobs: partially augmented — still missing {{{listed}}}"


def _write_bib(plans: list[Plan], args) -> int:
    bib_file = Path.cwd() / "references" / "refs.bib"
    if not bib_file.exists():
        print(f"[warn] {bib_file} not found — run `zobs` first", file=sys.stderr)
        return 0
    text = bib_file.read_text(encoding="utf-8")
    touched = 0
    for p in plans:
        marker = _marker(p)
        if not p.changes and marker is None:
            continue
        item = p.applied_item if p.changes else p.candidate.item
        text = _splice_bib(
            text,
            p.candidate.citekey,
            build_bib_entry(item, p.candidate.citekey),
            marker,
        )
        touched += 1
    bib_file.write_text(text, encoding="utf-8")
    return touched


def _push(zot, plan: Plan) -> bool:
    def send(item: dict) -> None:
        for f, (_, new) in plan.changes.items():
            item["data"][f] = new
        zot.update_item(item)

    try:
        send(json.loads(json.dumps(plan.candidate.item)))
        return True
    except zotero_errors.PreConditionFailedError:
        # the item changed since we fetched it — refetch, re-merge, retry once
        try:
            send(zot.item(plan.candidate.key))
            return True
        except Exception as e:  # noqa: BLE001 - report and move on
            print(f"  [err] Zotero push {plan.candidate.citekey}: {e}", file=sys.stderr)
            return False
    except Exception as e:  # noqa: BLE001
        print(f"  [err] Zotero push {plan.candidate.citekey}: {e}", file=sys.stderr)
        return False


def _has_write_access(zot) -> bool:
    try:
        info = zot.key_info()
    except Exception:  # noqa: BLE001 - fall through to the real update
        return True
    access = (info or {}).get("access", {}).get("user", {})
    return bool(access.get("write") or access.get("library"))


# ── entry point ─────────────────────────────────────────────────────────────


def run_augment(args) -> int:
    cfg = load_config()
    zot = zotero.Zotero(cfg["user_id"], "user", cfg["api_key"])

    try:
        cands = collect(cfg, zot)
    except (ValueError, RuntimeError, zotero_errors.HTTPError) as e:
        print(f"[error] {e}", file=sys.stderr)
        return 1

    if args.as_json:
        print(
            json.dumps(
                {
                    "candidates": [
                        {
                            "key": c.key,
                            "citekey": c.citekey,
                            "itemType": c.item_type,
                            "title": c.title,
                            "doi": c.doi or None,
                            "complete": c.complete,
                            "missing": c.missing,
                            "malformed": c.malformed,
                        }
                        for c in cands
                    ]
                },
                indent=2,
            )
        )
        return 0

    if cfg["bbt_url"] and args.no_zotero:
        print(
            "[error] ZOTERO_BBT_URL is set — refs.bib is Better BibTeX's export and "
            "cannot be edited here. Let augment push to Zotero (drop --no-zotero).",
            file=sys.stderr,
        )
        return 1

    chosen = select(cands, args)
    if not chosen:
        print("Nothing selected.")
        return 0

    cache = ResponseCache(
        Path.cwd() / "references" / ".zobs-cache" / "augment", refresh=args.refresh
    )
    mailto = os.environ.get("ZOBS_CONTACT_EMAIL") or None
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]

    print(f"Resolving {len(chosen)} entr{'y' if len(chosen) == 1 else 'ies'}...")
    plans = []
    for c in chosen:
        try:
            res = md.resolve(
                c.item["data"],
                sources=sources,
                cache=cache,
                min_ratio=args.min_confidence,
                mailto=mailto,
            )
        except Exception as e:  # noqa: BLE001 - a bad lookup must not abort the run
            print(f"  [warn] {c.citekey}: lookup failed ({e})", file=sys.stderr)
            res = Resolution(None, "none", "")
        plans.append(plan_changes(c, res, args))
    show_plans(plans)

    changed = [p for p in plans if p.changes]
    still_incomplete = [p for p in plans if p.still_missing]
    repaired = sum(1 for p in changed if p.candidate.malformed and not args.no_repair)

    if args.dry_run:
        print("\n(dry run — nothing written)")
        return 2 if still_incomplete else 0

    if not changed:
        touched = 0 if args.no_bib else _write_bib(plans, args)
        _summary(0, 0, len(still_incomplete), repaired, touched)
        return 2 if still_incomplete else 0

    if not args.yes:
        push = not args.no_zotero
        tail = f" and push {len(changed)} to Zotero" if push else ""
        if input(f"\nApply to refs.bib{tail}? [y/N] ").strip().lower() not in {
            "y",
            "yes",
        }:
            print("Aborted.")
            return 0

    pushed = failed = 0
    if not args.no_zotero:
        if not _has_write_access(zot):
            print(
                "[error] ZOTERO_API_KEY has no write access — enable it at "
                "zotero.org/settings/keys, or re-run with --no-zotero.",
                file=sys.stderr,
            )
            return 1
        for p in changed:
            if _push(zot, p):
                pushed += 1
            else:
                failed += 1

    touched = 0 if args.no_bib else _write_bib(plans, args)
    _summary(len(changed), pushed, len(still_incomplete), repaired, touched)
    return 2 if (still_incomplete or failed) else 0


def _summary(augmented, pushed, incomplete, repaired, bib_touched) -> None:
    print(
        f"\naugmented {augmented} (pushed {pushed})   "
        f"still incomplete {incomplete}   repaired {repaired}   "
        f"refs.bib entries rewritten {bib_touched}"
    )
