# `zobs augment` — fill in incomplete bibliography entries

Status: **implemented.** This document is the authoritative description of the
feature; keep it in sync with `src/zobs/augment.py`, `metadata.py`, `cache.py`
and `cli.py`. Decisions are locked in §10.

## 1. Problem

`zobs` builds `references/refs.bib` from whatever Zotero holds. Zotero items are
often thin — added from a browser button, an arXiv link, or a PDF drop — so the
generated entries are missing what a real citation needs. From the current
`variants/paper` bib:

```bibtex
@article{xiaoMVPDetectingVulnerabilities,
  title   = {MVP: Detecting Vulnerabilities using Patch-Enhanced Vulnerability Signatures},
  author  = {Xiao, Yang},          % truncated author list
  year    = {},                    % missing
  journal = {},                    % missing venue
  doi     = {},                    % missing
}

@article{caoVulPADetectingSemantically2025,
  ...
  year    = {June},                % date parsing bug: "June 2025"[:4]
  journal = {Proc. ACM Softw. Eng.},
}

@inproceedings{aleebrahimDNAAutomatedVulnerability2026,
  ...
  year      = {Apri},              % "April 2026"[:4]
}
```

Three separate defects:

1. **Missing fields** — venue, year, volume, issue, pages, DOI.
2. **Malformed values** — `year = {June}` from `(data["date"] or "")[:4]` in
   `build_bib_entry` (`sync.py:192`). A month name or ISO timestamp is sliced
   blindly.
3. **No enrichment path** — nothing pulls the missing data from anywhere.

`zobs augment` addresses all three: detect incomplete entries, let the user pick
which to fix, fetch canonical publication metadata from bibliographic APIs, write
it into `refs.bib`, and (when the API key allows) push it back to Zotero so the
fix persists across syncs.

## 2. Scope

In scope:

- Completeness check tuned to IEEE citation style, per Zotero item type.
- Interactive selection of which incomplete entries to augment (default: all
  incomplete, with opt-out per entry and opt-in for complete ones).
- Metadata resolution from Crossref, DataCite, DBLP, OpenAlex, arXiv.
- Merge into the local `.bib` (fill blanks; never clobber good data by default).
- Optional write-back to Zotero via `zot.update_item`.
- A machine-readable mode so Claude drives the checkbox selection.

Out of scope for v1 (noted in phasing):

- Rewriting author lists / fixing name formatting (`--fix-authors`, later).
- Non-IEEE citation styles.
- Creating new Zotero items or fetching PDFs.

## 3. User-facing behaviour

New subcommand. `zobs` with no arguments still runs the sync exactly as today
(back-compat); `zobs augment` is additive.

```
zobs augment [selection] [mode] [options]

selection:
  (default)              all entries missing IEEE-required fields
  --select KEY[,KEY...]  only these items (Zotero keys or citekeys)
  --all                  every entry, complete or not
  --include-complete     add complete entries to the interactive list, unchecked

mode:
  (default, TTY)         interactive checklist -> preview -> confirm -> apply
  (default, non-TTY)     behaves as --dry-run unless --select/--all + --yes given
  --json                 print candidates as JSON and exit (no enrichment calls)
  --dry-run              resolve + show the diff, write nothing
  --yes                  skip the confirmation prompt (for scripted / Claude use)

options:
  --no-zotero            update refs.bib only, do not push to Zotero
  --no-bib               push to Zotero only, do not touch refs.bib
  --overwrite            replace existing non-empty fields too (default: fill blanks)
  --repair               treat obviously-malformed values (year="June") as blank
                         (default: on)
  --min-confidence 93    token_sort_ratio bar for entries without a usable DOI
  --sources LIST         comma list / order (default: crossref,datacite,dblp,openalex,arxiv)
  --refresh              ignore the local API response cache
```

### Interactive checklist (standalone use, TTY)

```
Incomplete entries (IEEE-required fields missing):

 [x] 1. xiaoMVPDetectingVulnerabilities        missing: venue, year, pages, doi
 [x] 2. caoVulPADetectingSemantically2025      malformed: year   missing: volume, pages
 [x] 3. aleebrahimDNAAutomatedVulnerability2026 malformed: year   missing: pages
 [ ] 4. bishopComputerSecurityArt2018          (book — venue N/A)  missing: publisher

Toggle: numbers to flip, 'a' all, 'n' none, Enter to accept >
```

Rendered with `questionary` (checkbox prompt). Falls back to a plain numbered
toggler when stdin is not a TTY but `--select` was not given.

### Claude-driven flow (primary use — "checkboxes like Claude can")

1. Claude runs `zobs augment --json`:

   ```json
   {
     "collection": "SIF9PW9T",
     "candidates": [
       {
         "key": "ABCD1234",
         "citekey": "xiaoMVPDetectingVulnerabilities",
         "itemType": "conferencePaper",
         "title": "MVP: Detecting Vulnerabilities using Patch-Enhanced ...",
         "doi": null,
         "complete": false,
         "missing": ["proceedingsTitle", "date", "pages", "DOI"],
         "malformed": []
       },
       { "...": "..." }
     ]
   }
   ```

   This is fast — one Zotero collection fetch (~3 s after the batch-fetch change),
   no enrichment API calls.

2. Claude presents `AskUserQuestion` with `multiSelect: true`, one option per
   incomplete candidate, all pre-selected, the `missing`/`malformed` list as each
   option's description. The user unchecks any they want to leave alone.

3. Claude runs `zobs augment --select KEY1,KEY2,... --dry-run` and shows the
   resolved field-by-field diff.

4. On the user's OK, Claude runs `zobs augment --select KEY1,KEY2,... --yes`.

Later this can be wrapped in a `/augment-bib` slash command or an
`augment-bib` skill so the round-trip is one step (phase 4).

## 4. Completeness model

Source of truth is the **Zotero item data**, not the parsed `.bib` — augment
needs the item `version` and `itemType` for write-back anyway, and structured
data makes malformed-value detection reliable.

Required fields for "IEEE-complete", by item type (Zotero field names):

`venue` below is any of `publicationTitle` / `proceedingsTitle` /
`conferenceName`. `year` is `year_from_date(date)`. `arxivID` is
`arxiv_id_from_item()` or any DOI. (`augment.REQUIRED`)

| itemType         | required (`missing_fields`)                     | filled when found, not demanded |
|------------------|------------------------------------------------|---------------------------------|
| `journalArticle` | `creators`, `title`, `venue`, `year`, `pages`   | `volume`, `issue`, `DOI`, `ISSN` |
| `conferencePaper`| `creators`, `title`, `venue`, `year`, `pages`   | `DOI`, `place`, `publisher` |
| `preprint`       | `creators`, `title`, `year`, `arxivID`          | `DOI`, `repository` |
| `book`           | `creators`, `title`, `year`, `publisher`        | `place`, `ISBN` |
| `report`         | `creators`, `title`, `year`                     | `publisher`, `number` |
| `webpage`        | `title`, `year`, `url`                          | `accessDate` |
| _(other)_        | `creators`, `title`, `year`                     | — |

`volume`/`issue` are not required: too many conference papers are misfiled as
`journalArticle`, and demanding a volume they will never have just produces
noise. Augment still fills them from the resolved record.

- `missing_fields(data) -> list[str]` — required field absent or empty.
- `malformed_fields(data) -> list[str]` — present but unusable:
  - `date` that yields no 4-digit year via `year_from_date()`
  - `pages` in `{"n. pag.", "-", "–", "—", "none", "in press"}`
- `complete` = `missing_fields` empty **and** `malformed_fields` empty.
- Complete entries are skipped by default (the user's requirement). `--all` or
  `--include-complete` overrides.

`year_from_date(s)` in `sync.py` — **done**, replaced the `[:4]` slice. First
`\b(?:19|20)\d{2}\b` match, `""` when none. Corrected 15 mangled years
(`"June 2025"`, `"2026-07-15T00:22:32Z"`, `"5/20/2017"`, `"01/2025"`, …) on the
first `variants/paper` run.

## 5. Metadata resolution

### Per-entry pipeline

```
item
 │
 ├─ has Crossref DOI (10.1145, 10.1109, 10.1007, 10.1145, 10.1002, ...)
 │     → Crossref  GET /works/{doi}                → authoritative, auto-apply
 │
 ├─ has DataCite DOI (10.48550 arXiv, 10.5281 Zenodo, ...)
 │     → DataCite  GET /dois/{doi}   (+ arXiv API for 10.48550)  → auto-apply
 │
 └─ no usable DOI
       → search DBLP → Crossref → OpenAlex → arXiv  (first high-tier hit wins)
         per candidate: token_sort_ratio, author_ok, year_ok, key_ok  (see §10)
           ratio ≥ 93 + author + year + key-term → auto-apply (high)
           ratio ≥ 80 + author + key-term        → shown, batch 'y' to include (low)
           otherwise                             → no-match marker (§10.5)
```

Rationale for multiple sources: **Crossref alone is not enough for this field.**
Confirmed while writing this spec — "MVP: Detecting Vulnerabilities using
Patch-Enhanced Vulnerability Signatures" (USENIX Security 2020) is absent from
Crossref entirely. USENIX, NDSS, and some IEEE S&P papers need DBLP or OpenAlex.

### Sources

| source   | endpoint                                                   | key | best for |
|----------|-----------------------------------------------------------|-----|----------|
| Crossref | `api.crossref.org/works/{doi}` , `/works?query.bibliographic=` | none | journals, ACM/Springer/Elsevier confs |
| DataCite | `api.datacite.org/dois/{doi}`                              | none | arXiv (`10.48550`), Zenodo, figshare |
| arXiv    | `export.arxiv.org/api/query?id_list=` / `search_query=`    | none | preprints (canonical `arXiv:ID`, version, date) |
| DBLP     | `dblp.org/search/publ/api?q=&format=json`                  | none | USENIX, NDSS, IEEE S&P, CCS, CS venues generally |
| OpenAlex | `api.openalex.org/works/doi:{doi}` , `/works?filter=title.search:` | none | catch-all aggregator, `host_venue`, `biblio` |

Polite API use:

- Descriptive `User-Agent: zobs/<version> (+https://github.com/.../zobs)`.
- Crossref "polite pool": include `mailto=` **only** if the user sets
  `ZOBS_CONTACT_EMAIL` (opt-in, default off).
- Honour `Retry-After` / `X-Rate-Limit` / DBLP's `429`; 250 ms min spacing.
- Cache every raw response at `references/.zobs-cache/augment/<sha1(url)>.json`,
  no TTL (bibliographic records are stable), `--refresh` to bust. Consuming
  projects gitignore `references/.zobs-cache/`.

### Normalised record (internal)

Each source adapter returns:

```python
@dataclass
class Metadata:
    container_title: str | None      # journal or proceedings name
    container_short: str | None      # "Proc. ACM Softw. Eng."
    volume: str | None
    issue: str | None
    pages: str | None                # "2430-2453"
    year: int | None
    month: int | None
    doi: str | None
    issn: str | None
    isbn: str | None
    publisher: str | None
    event_name: str | None           # conference name
    event_place: str | None
    arxiv_id: str | None
    work_type: str | None            # journal-article | proceedings-article | preprint | book | ...
    authors: list[tuple[str, str]]   # (given, family)
    source: str                      # which adapter produced this
    score: float                     # 1.0 for DOI hits
```

## 6. Field mapping

### Crossref `message` → `Metadata`

| Crossref                         | Metadata |
|----------------------------------|----------|
| `container-title[0]`             | `container_title` |
| `short-container-title[0]`       | `container_short` |
| `volume`                         | `volume` |
| `issue`                          | `issue` |
| `page`                           | `pages` |
| `issued.date-parts[0]` `[y,m,d]` | `year`, `month` |
| `DOI`                            | `doi` |
| `ISSN[0]`                        | `issn` |
| `ISBN[0]`                        | `isbn` |
| `publisher`                      | `publisher` |
| `event.name` / `event.location`  | `event_name` / `event_place` |
| `type`                           | `work_type` |
| `author[].{given,family}`        | `authors` |

### `Metadata` → BibTeX (extended `build_bib_entry`)

`@article`: `title, author, journal (=container_title), year, month, volume,
number (=issue), pages, doi, issn`
`@inproceedings`: `title, author, booktitle (=container_title), year, month,
volume, pages, publisher, address (=event_place), doi`
preprint (stays `@article`, §10.6): `title, author, year,
journal = {arXiv preprint arXiv:<id>}, eprint = {<id>}, archivePrefix = {arXiv},
primaryClass, doi`

Only non-empty fields are emitted. Field order fixed for stable diffs.

### `Metadata` → Zotero item data (for `update_item`)

| Metadata          | `journalArticle` | `conferencePaper` | `preprint` |
|-------------------|------------------|-------------------|------------|
| `container_title` | `publicationTitle` | `proceedingsTitle` | — |
| `container_short` | `journalAbbreviation` | — | — |
| `event_name`      | — | `conferenceName` | — |
| `event_place`     | — | `place` | — |
| `volume`          | `volume` | `volume` | — |
| `issue`           | `issue` | `issue` | — |
| `pages`           | `pages` | `pages` | — |
| `year`/`month`    | `date` (`YYYY-MM` or `YYYY`) | same | same |
| `doi`             | `DOI` | `DOI` | `DOI` |
| `issn`            | `ISSN` | `ISSN` | — |
| `isbn`            | — | `ISBN` | — |
| `publisher`       | `publisher` | `publisher` | — |
| `arxiv_id`        | — | — | `archiveID` (`arXiv:<id>`), `repository = arXiv` |

Field names verified against `zot.item_template(...)` for each type.

## 7. Applying changes

### Local `.bib`

- Default (zobs-generated bib): re-run entry generation for the touched citekeys
  with `Zotero data ⊕ Metadata`, splice back into `refs.bib`, leave every other
  entry and the ordering untouched.
- `ZOTERO_BBT_URL` mode: `refs.bib` is Better BibTeX's export and zobs must not
  fight it. In this mode augment **requires** a Zotero push (`--no-bib` is
  implied) — once Zotero has the data, the next sync re-pulls the corrected
  export. If `--no-zotero` is also given, error out with an explanation.
- Merge rule: fill only empty/malformed fields. `--overwrite` replaces
  non-empty fields when the source is a DOI hit (score 1.0); never on a fuzzy
  match.

### Zotero push

- Needs an API key with **write** access. Detect up front: a trial
  `zot.update_item` returning 403 → stop with
  `"ZOTERO_API_KEY lacks write access — enable it at zotero.org/settings/keys"`.
- Reuse the item dict from the collection fetch (already carries `version`).
  Merge the mapped fields into `item["data"]`, then `zot.update_item(item)`
  (pyzotero sends `If-Unmodified-Since-Version`).
- `412 Precondition Failed` (item changed since fetch) → re-fetch that one item,
  re-merge, retry once; still failing → skip it, report at the end.
- Do **not** write `citationKey` (Better BibTeX owns it) or touch `creators` in
  v1.
- `--dry-run` prints the per-item JSON patch.
- After a successful push (non-BBT mode) regenerate the bib from the merged data
  in memory — no second Zotero round-trip.

## 8. Output

```
Resolving 3 entries...

  xiaoMVPDetectingVulnerabilities              [DBLP, score 0.97]
    proceedingsTitle  (empty)  ->  31st USENIX Security Symposium (USENIX Security 22)
    date              (empty)  ->  2020
    pages             (empty)  ->  1165-1182
    DOI               (empty)  ->  (none available)

  caoVulPADetectingSemantically2025            [Crossref, DOI]
    date              June     ->  2025-06
    volume            (empty)  ->  2
    issue             (empty)  ->  FSE
    pages             (empty)  ->  2430-2453

  yunCLEARCausalContextBased2026a              no confident match (best: DBLP 0.71) — skipped

Apply to refs.bib and push 2 items to Zotero? [y/N]
```

Summary line and exit codes:

```
augmented 2  (pushed 2)   low-confidence 0   unmatched 1   malformed-repaired 1
```

| exit | meaning |
|------|---------|
| 0    | all selected entries resolved and applied |
| 1    | config / auth / network error |
| 2    | completed, but some entries unmatched or Zotero pushes failed |

## 9. Implementation plan

New code:

```
src/zobs/
  cli.py          # argparse: `zobs` (sync, default) + `zobs augment`
  augment.py      # completeness model, selection, orchestration, bib splice
  metadata.py     # Metadata dataclass, source adapters, matching/scoring
  cache.py        # tiny keyed JSON response cache (shared, also usable by sync)
```

`pyproject.toml`: `zobs = "zobs.cli:main"`; add `rapidfuzz` and `questionary` to
`dependencies`. HTTP stays on `urllib` (matching `fetch_bbt_bib`); arXiv Atom
parsed with `xml.etree`.

`sync.py` refactor: `year_from_date()` is already extracted (bug fix). Also pull
the field-emitting core of `build_bib_entry()` into the shared generator so
`augment` and `sync` produce identical formatting.

Tests: recorded JSON/XML fixtures per source under `tests/fixtures/`; unit tests
for completeness detection, `year_from_date`, matching thresholds, bib splice
(idempotent), and Zotero patch construction with a mocked `update_item`
(including the 412 retry).

### Phasing

| phase | deliverable |
|-------|-------------|
| 1 | `cli.py` + `zobs augment --json` + completeness model + Crossref-by-DOI + local bib splice + `--dry-run`. Enough for the Claude checkbox flow on DOI-bearing entries. |
| 2 | Zotero write-back (`update_item`, 412 handling, write-access check). |
| 3 | No-DOI resolution: DBLP + OpenAlex + DataCite + arXiv adapters, scoring, confidence tiers. |
| 4 | Standalone interactive checklist; `/augment-bib` slash command / skill wrapping the `--json` → `AskUserQuestion` → `--select --yes` round-trip. |

## 10. Decisions (locked)

1. **Subcommand.** `zobs augment ...`. `sync.py` gains an `argparse` front end in a
   new `cli.py`; bare `zobs` still runs the sync.
2. **Dependencies added.** `rapidfuzz` (title matching) and `questionary` (the
   standalone checklist). Both real deps, not optional extras.
3. **Crossref polite pool** — opt-in only, `mailto=` sent when `ZOBS_CONTACT_EMAIL`
   is set. Default off.
4. **Confirm once per batch.** Show the full resolved diff, then a single
   `[y/N]`. No per-entry prompting for DOI hits. `--yes` skips it.
5. **No-match marker.** An unmatched entry gets a comment line written into
   `refs.bib` directly above it:
   `% zobs: no confident match — still missing {proceedingsTitle, pages}`
   Removed automatically on a later run that does resolve it. No separate report
   file.
6. **Preprints stay `@article`** with `journal = {arXiv preprint arXiv:<id>}`,
   plus `eprint` / `archivePrefix = {arXiv}` / `primaryClass` fields. This is the
   de-facto CS convention (Google Scholar export), renders under both `plain`
   (current `main.tex` bibstyle) and `IEEEtran`, and changes no `\cite` calls.
7. **Repair is on by default** (`--repair`, opt-out with `--no-repair`).
   Obviously-malformed values (`year = "June"`, `pages = "n. pag."`) are treated
   as empty and refilled. This is not `--overwrite` — well-formed existing values
   are still left alone.
8. **Zotero write scope:** IEEE-required fields plus `DOI`, `ISSN`/`ISBN`,
   `publisher`, and the arXiv id. Not abstract/URL/keywords in v1.
9. **Per-entry selection only.** No per-field checkboxes.

### Matching accuracy (decision 4 detail — the academic-standard bar)

Identifier first, then a strict check. Signals per candidate (`metadata.Match`):

- `ratio` — `rapidfuzz.fuzz.token_sort_ratio` of the normalised titles (0–100).
  `token_sort` not `token_set`: `token_set` dedupes and drops extra tokens, so a
  3-word title like "V1SCAN Discovering 1day" scored ~93 against unrelated
  workshop papers. `token_sort` does not.
- `author_ok` — first-author surname matches.
- `year_ok` — `|year_pub − year_have| ≤ 1`, or the local item has no year.
- `key_ok` — the **key-term gate**: if the local title leads with a system name
  (`MOVERY:`, `V1SCAN -`), the candidate title must contain it. This is what
  rejects the VUDDY-for-MOVERY and APSEC-for-V1SCAN false hits.

| tier | rule | action |
|------|------|--------|
| exact | resolvable DOI / arXiv id → source lookup returns non-empty | apply |
| high | `ratio ≥ --min-confidence` (default 93) **and** `author_ok` **and** `year_ok` **and** `key_ok` | apply |
| low | `ratio ≥ 80` **and** `author_ok` **and** `key_ok` | shown in the diff; included only on the batch `y` |
| none | anything else | no-match marker (decision 5) |

Implementation note: a source lookup is best-effort. If DBLP or Crossref is
rate-limited or drops the connection, that source silently yields nothing and
the entry may land in a lower tier; re-running picks it up (successful responses
are cached, and requests are spaced ~0.34 s apart).
