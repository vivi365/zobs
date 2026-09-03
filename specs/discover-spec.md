# `zobs discover`: find what your collection keeps citing but does not hold

Status: **implemented (v1).** This document is the authoritative description of
the feature; keep it in sync with `src/zobs/discover.py`, `metadata.py`,
`cache.py` and `cli.py`. Decisions are locked in §9.

## 1. Problem

Every paper in the selection has a reference list. Those reference lists overlap,
and the overlap is informative: a paper that eight of your sixty papers cite,
which you do not hold, is almost certainly something you were supposed to have
read. Finding it today means opening sixty bibliographies and keeping a tally by
hand, so nobody does it, and the gap stays open until a reviewer points at it.

This is backward snowballing, the standard first step of a systematic literature
review. `zobs` already knows the exact set of papers to snowball from, already
resolves them against bibliographic APIs, and already caches the responses. The
missing piece is small: read the reference lists it is already fetching, count
what they point at, and subtract what the user already has.

Two things make the naive version useless, and both are addressed below:

1. **Raw counts rank famous papers first.** The most-cited work in any
   sub-literature is the textbook or the seminal paper everyone cites and nobody
   reads at this stage. Those are exactly the papers the user has already decided
   not to hold, and they will crowd out the specific ones every run.
2. **Rejected suggestions come back.** A discovery tool re-run weekly that
   re-proposes the same eight papers you already dismissed is abandoned by the
   third run.

## 2. Scope

In scope for v1:

- Resolve each selection item to an OpenAlex work and read its `referenced_works`.
- Count references across the selection, keeping the citing citekeys as evidence.
- Exclude anything already in the selection and anything on the ignore list.
- Rank by local concentration rather than raw count.
- A ranked table, a `--json` mode, and a persistent ignore list.
- Honest coverage reporting.

Out of scope for v1 (see the phasing table, §8):

- Creating Zotero items from the results, writing `refs.bib`, fetching PDFs.
  **v1 is strictly read-only** apart from the ignore file.
- Semantic Scholar as a second citation-graph source.
- Forward snowballing (who cites *your* papers).
- Author, venue or recency filters.

## 3. User-facing behaviour

New subcommand. `zobs` with no arguments still runs the sync, and `zobs augment`
is untouched.

```
zobs discover [options]

  --limit N          how many suggestions to print (default: 25)
  --min-count N      only suggest papers cited by at least N of yours (default: 2)
  --json             print the ranked suggestions as JSON
  --refresh          ignore the local API response cache
  --ignore KEY       add an OpenAlex id or DOI to references/.zobs-ignore
                     and exit (repeatable)
```

Default output:

```
Reading the reference lists of 61 papers...
Resolved 54/61 selection items; 48 had reference data (1904 references read).

25 papers cited by at least 2 of your papers but not in your collection:

   #  cited   global  year  first author      venue                 title
   1      3      614  2006  Li                IEEE Transactions o…  CP-Miner: finding copy-paste and related bugs…
      via jangReDeBug2012, kimVUDDY2017, liVulPecker2016
      doi 10.1109/tse.2006.28
   2      2      119  2010  Pham                                    Detection of recurring software vulnerabilities
      via kimVUDDY2017, liVulPecker2016
      doi 10.1145/1858996.1859089

Not interested in one? `zobs discover --ignore 10.1109/tse.2006.28` keeps it
out of future runs.
```

`cited` is how many of the user's own papers cite this one; `global` is
OpenAlex's `cited_by_count`. The `via` line is the point of the feature: a bare
count is a recommendation, a count plus "these three of your papers cite it" is
a claim the user can check in thirty seconds. When a candidate has no DOI, the
OpenAlex id is printed instead so it can still be fed to `--ignore`.

`--json` runs the same pipeline and prints the ranked results with the full
`via` list per candidate, untruncated. Note that this differs from
`zobs augment --json`, which deliberately makes no network calls: for discover
the network work *is* the feature, so `--json` is a formatting choice, not a
fast path.

## 4. Resolution

Each selection item must become an **OpenAlex** work, because OpenAlex is the
only keyless source among the five that exposes a machine-readable reference
list. This is the one place discover deliberately does not reuse
`metadata.resolve()`:

> `resolve()` returns the first good hit from any configured source, and it
> tries DBLP first because DBLP has the best coverage of the CS venues this user
> reads. A DBLP or Crossref hit is the right answer for `augment` and the wrong
> answer here: it carries no OpenAlex id, so it carries no reference list.

The per-item pipeline is therefore OpenAlex-only:

```
item
 │
 ├─ has a DOI
 │     → GET /works/doi:{doi}
 │       one exact request, and the response already contains
 │       `referenced_works`, so the bibliography costs no extra call
 │
 └─ no DOI
       → GET /works?filter=title.search:{title}
         score each hit with metadata.score(): ratio, author_ok, year_ok, key_ok
           ratio ≥ 93 and author_ok and year_ok and key_ok  → accept (high)
           anything else                                    → unresolved
```

**The low tier is never accepted, and there is no flag to lower the bar.** In
`augment` a low-tier match costs one wrong field on one entry, visible in the
diff before it is applied. Here a wrong match injects that paper's entire
reference list, forty or fifty citations, into a ranking the user cannot audit.
One wrong resolution poisons every row below it. The asymmetry justifies being
stricter than `augment` rather than merely as strict.

Unresolved items are counted and reported, not silently dropped (§6).

### Cache

Discover uses **augment's cache directory**, `references/.zobs-cache/augment`,
rather than one of its own. The cache is keyed by request URL, so an OpenAlex
record either command has fetched is reused by the other for free. A user who
has run `zobs augment` over the collection has already paid for most of
discover's DOI lookups. The directory name is now a slight misnomer; renaming it
would invalidate every existing user's cache for no benefit, so it stays, with a
comment at the constant. `--refresh` bypasses the cache exactly as in `augment`.

## 5. Ranking

The index is built as `openalex_id -> set of citekeys in the collection that
reference it`, never as an integer counter. The set is the evidence, it is what
the `via` line prints and what `--json` exports, and it costs nothing to keep.

A candidate must clear `--min-count` (default 2). A single citation is noise:
every paper in the selection cites thirty things once, and the tail is mostly
tangential.

Ranking is **not** raw count:

```
score = count / log10(10 + cited_by_count)
```

where `count` is how many of the user's papers cite the candidate and
`cited_by_count` is OpenAlex's global citation count.

The divisor runs from 1.0, for an uncited or unknown work, to about 4.6 at
40,000 citations, so the global count reorders candidates without ever
overwhelming the local evidence. Concretely:

| candidate                              | count | global | score |
|----------------------------------------|-------|--------|-------|
| niche paper, cited by 5 of your 60      | 5     | 200    | 2.15  |
| famous paper, cited by the same 5       | 5     | 40,000 | 1.09  |
| famous paper, cited by 10 of your 60    | 10    | 40,000 | 2.17  |

The first two rows are the requirement: at equal local evidence, prefer the
paper that is specific to what the user is reading over the one that everybody
cites. The third row is the guard rail: doubling the local evidence still wins,
so the weighting reorders rather than inverts. A work with an unknown or zero
global count is not penalised, since `log10(10) = 1`.

Ties break on raw count, then on the *lower* global count, then on title, so
runs are reproducible.

The formula lives in one function, `discover.rank_score(count, cited_by_count)`.
Swapping it is the supported way to change the ranking.

### Why the whole pool is fetched, not the top N by count

The metadata fetch is batched (§7), and the obvious thing to batch is the top
`--limit` candidates by raw count. That would be wrong: the ranking exists
precisely to promote candidates that raw count ranks low, and truncating on raw
count first would discard exactly the niche papers the weighting is meant to
surface. So every candidate at or above `--min-count` is fetched, in batches,
and `--limit` is applied after ranking. A `MAX_LOOKUPS` ceiling of 400
candidates, applied after sorting by count, keeps a very large selection from
turning into a hundred requests.

## 6. Exclusion and coverage

**Exclusion** is by OpenAlex id *and* by DOI. The id catches the common case.
The DOI catches the item the user holds but which discover could not resolve to
an OpenAlex work, and the candidate whose id differs from the held record's id
because OpenAlex merged two entries. Id exclusion happens before the metadata
fetch; DOI exclusion necessarily happens after it, since the candidate's DOI is
one of the things the fetch returns.

**Coverage is reported on every run**, before the results:

```
Resolved 54/61 selection items; 48 had reference data (1904 references read).
```

Two numbers, because they fail independently: an item can resolve to an OpenAlex
work that has no reference list at all. OpenAlex's reference coverage is
markedly weaker for USENIX Security and NDSS than for ACM and IEEE venues, which
is precisely the gap that matters for this user's field, so a thin result is
often a statement about the data rather than about the user's reading. Printing
`54/61` makes that visible. When there are no results at all and coverage was
incomplete, the empty-result message says so explicitly rather than implying the
collection is complete.

Known limitation, not currently surfaced: OpenAlex silently returns nothing for
ids that have been merged or withdrawn, so a batch of 46 ids may come back with
38 works. Those candidates are dropped, because there is no metadata to display.
Observed loss is roughly 10 to 20 percent of referenced ids.

## 7. Requests

| call | endpoint | cost |
|------|----------|------|
| resolve by DOI | `/works/doi:{doi}` | 1 per item, returns the reference list too |
| resolve by title | `/works?filter=title.search:{title}` | 1 per DOI-less item |
| candidate metadata | `/works?filter=openalex_id:W1\|W2\|...` | 1 per 50 candidates |

The id filter takes an OR-list capped at 50 values (`metadata.OPENALEX_BATCH`),
so 300 candidates cost 6 requests rather than 300. All three go through
`ResponseCache`, which spaces live requests about 0.34 s apart and stores
responses with no expiry.

A representative 61-item collection: about 61 resolution requests on the first
run (near zero afterwards, and near zero from the start if `augment` has been
run), plus 4 to 8 batched candidate requests.

## 8. Phasing

| phase | deliverable |
|-------|-------------|
| 1 (this PR) | `Metadata.openalex_id` / `referenced_works` / `cited_by_count`, OpenAlex-only resolution, the citation index, weighted ranking, ignore list, ranked table and `--json`. Read-only. |
| 2 | Interactive picker over the results that creates the chosen papers as Zotero items (`zot.create_items` from the OpenAlex record), so a discovery flows straight into the next `zobs` sync. Needs a write-scoped key, and the same `--dry-run` and confirmation discipline `augment` uses. |
| 3 | Semantic Scholar as a second citation-graph source. Better CS coverage than OpenAlex, keyless, and its `references` field fills exactly the USENIX and NDSS gap §6 describes. Merge the two graphs on DOI before counting. |
| 4 | Forward snowballing (`cited_by`), to catch work published after the papers in the collection. |

## 9. Decisions (locked)

1. **OpenAlex-only resolution, exact and high tiers only.** No `--min-confidence`
   flag, no low tier. §4 gives the asymmetry argument.
2. **Shared cache directory** with `augment`, `references/.zobs-cache/augment`.
   Not renamed in this PR.
3. **Evidence, not counters.** The index maps to a set of citekeys, and the
   citing papers are shown in both output modes.
4. **`score = count / log10(10 + cited_by_count)`**, in one swappable function.
5. **`--min-count` defaults to 2.** One citation is noise.
6. **Ignore list at `references/.zobs-ignore`**, one OpenAlex id or DOI per line,
   `#` comments allowed, **committed to the repo** unlike the cache. Keys are
   normalised on both read and write, so `W123`, `w123` and
   `https://openalex.org/W123` are one key, as are `10.1145/x`,
   `https://doi.org/10.1145/X` and `doi:10.1145/x`. Only a whitespace-preceded
   `#` opens a comment, since a DOI may legally contain one.
7. **`--ignore` appends and exits**, without running discovery. The feedback is
   immediate and unambiguous, and the follow-up `zobs discover` is nearly free
   off the cache.
8. **Read-only in v1.** No Zotero writes, no `refs.bib` writes, no PDF fetching.
   The ignore file is the only thing discover creates.
9. **Coverage is always printed**, never suppressed, including in the
   no-results case.
10. **Citation-graph fields are excluded from `Metadata.is_empty()`.** Adding
    `openalex_id`, `cited_by_count` and `referenced_works` to the dataclass would
    otherwise make a record carrying nothing but an OpenAlex id read as non-empty,
    and `augment` uses `is_empty()` to decide whether a lookup succeeded. They
    describe the work's place in the literature, not the citation, so they do not
    count toward it and `augment`'s behaviour is unchanged.
