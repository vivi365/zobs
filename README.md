# zobs

Sync a Zotero selection (collection or tag) and Obsidian notes into your project workspace.

- Symlinks PDFs from local Zotero storage into `references/papers/`
- Generates `references/refs.bib`
- Links paper notes into `references/notes/` (from Obsidian, or Zotero's built-in notes)

## Setup and Usage

```bash
uv add zobs
```

Use the `.env.example` to create your `.env` config


| Variable | Required | Description |
|---|---|---|
| `ZOTERO_USER_ID` | yes | Numeric Zotero user ID (visible in your Zotero profile URL) |
| `ZOTERO_API_KEY` | yes | Create at zotero.org/settings/keys |
| `ZOTERO_SYNC_MODE` | no | `collection` (default) or `tag` |
| `ZOTERO_COLLECTION` | yes (mode=collection) | Collection name or 8-char key (ID) to sync |
| `ZOTERO_TAG` | yes (mode=tag) | Tag name or comma-separated list (AND logic) |
| `ZOTERO_STORAGE` | no | Path to Zotero local storage (default: `~/Zotero/storage`) |
| `ZOTERO_BBT_URL` | no | Better BibTeX local export URL (overrides built-in BibTeX generation) |
| `OBSIDIAN_NOTES` | no | Path to your Obsidian paper-summaries folder (see below) |

> API key and user ID are retrieved from [zotero.org/settings/keys](https://www.zotero.org/settings/keys). The collection id can be seen in the last part of URL in the Zotero web interface, e.g. `https://www.zotero.org/<username>/collections/<collection-id>/collection` or simply use the plaintext leaf-level name. For tag mode, set `ZOTERO_SYNC_MODE=tag` and provide one or more tags in `ZOTERO_TAG` (comma-separated; AND logic).

Sync pdfs and notes:
```bash
uv run zobs
```

---

## Augmenting incomplete entries

`zobs augment` looks for refs.bib entries that are missing what an IEEE citation
needs (venue, year, pages, DOI) and fills them in from public bibliographic
data. It updates `refs.bib`. If `ZOTERO_API_KEY` can write, it also saves the
same fields to Zotero so the next sync keeps them.

```bash
uv run zobs augment              # pick which incomplete entries to fill, interactively
uv run zobs augment --dry-run    # show what would change, write nothing
uv run zobs augment --json       # list incomplete entries as JSON (for scripts / agents)
uv run zobs augment --select KEY1,KEY2   # target specific Zotero keys or citekeys
```

By default it lists only the incomplete entries, with every one checked; uncheck
any you want to skip. A field that already has a sensible value is left alone
unless you pass `--overwrite`.

### How an entry is resolved

If the entry has a DOI, `zobs augment` reads it directly and uses that. Normal
DOIs come from Crossref; arXiv and Zenodo DOIs (`10.48550`, `10.5281`) come from
DataCite.

If there is no DOI, `zobs augment` searches by title. It tries DBLP first
(DBLP has the best coverage of USENIX, NDSS, S&P and CS venues generally), then
Crossref, then OpenAlex, then arXiv, and stops at the first good result.

A result is good when the title is close enough, the first author's surname
matches, and the year is within one. If the entry title starts with a tool name
like `MOVERY:`, that name must also appear in the result; this is what stops a
`MOVERY` entry from being filled in with the `VUDDY` paper. Pass
`--sources dblp,crossref,openalex` to shorten or reorder the list.

If no source returns a good result, `zobs augment` leaves the entry as it is and
writes a `% zobs: no confident match ...` line above it in `refs.bib`.

Set `ZOBS_CONTACT_EMAIL` to your own email address to join the Crossref "polite
pool" (Crossref asks for a contact address and gives those requests better rate
limits). It is optional and off by default.

Add `references/.zobs-cache` to `.gitignore`; that is where `zobs augment` keeps
its cache of API responses.

---

## Finding papers you are missing

`zobs discover` reads the reference list of every paper in your selection and
looks for papers that several of them cite but that you do not have. If five of
your sixty papers cite something and you have never added it, that is usually
worth a look. This is backward snowballing, done for you.

```bash
uv run zobs discover                  # ranked list of what you are missing
uv run zobs discover --min-count 4    # only things cited by 4+ of your papers
uv run zobs discover --limit 50       # show more
uv run zobs discover --json           # same results as JSON, for scripts / agents
```

Output looks like this:

```
Resolved 54/61 selection items; 48 had reference data (1904 references read).

   #  cited   global  year  first author      venue                 title
   1      3      614  2006  Li                IEEE Transactions o…  CP-Miner: finding copy-paste and related bugs…
      via jangReDeBug2012, kimVUDDY2017, liVulPecker2016
      doi 10.1109/tse.2006.28
```

`cited` is how many of your own papers cite it, `global` is its total citation
count everywhere. The `via` line names which of your papers cite it, so you can
check the suggestion instead of taking it on faith.

Results are not ranked by raw count. Raw count puts the famous papers on top,
and those are usually the ones you have already decided to skip. A paper cited
by 5 of your 60 with 200 citations worldwide ranks above one cited by the same 5
with 40,000, because the first is specific to what you are reading and the
second is cited by everyone.

The first line is the honest version of how much was actually read. OpenAlex
does not have reference lists for every paper, and its coverage of USENIX and
NDSS is thinner than its coverage of ACM and IEEE, so a short list sometimes
means missing data rather than nothing to find.

### Dismissing suggestions

```bash
uv run zobs discover --ignore 10.1109/tse.2006.28
```

That adds the paper to `references/.zobs-ignore` and it will not come back.
Takes a DOI or an OpenAlex id, and can be repeated. Unlike the cache, this file
is meant to be committed, so a decision you make once holds on every machine.

`zobs discover` only reads. It does not add anything to Zotero, touch
`refs.bib`, or download PDFs. It shares the cache with `zobs augment`, so if you
have already run `augment` over the collection most of the lookups are free.

---

## Obsidian integration (optional)

Without `OBSIDIAN_NOTES`, the package still works: PDFs sync, `refs.bib` is
generated using raw 8-char Zotero keys as citekeys, and any notes written
directly in Zotero (child notes on items) are exported to `references/notes/zotero/`.

If `ZOTERO_BBT_URL` is set, `refs.bib` is pulled from Better BibTeX instead of
being generated by `zobs`.

With `OBSIDIAN_NOTES`, the package additionally:

- Reads citekeys from your Obsidian notes (Better BibTeX author-year format)
- Symlinks matching notes into `references/notes/obsidian/`

### Required Obsidian plugin

Notes must be imported into Obsidian via the
[Zotero Integration](https://github.com/mgmeyers/obsidian-zotero-integration)
plugin (by mgmeyers).

Only notes with `zotero_key` set in the frontmatter are linked. This keeps
`references/notes/` scoped to the current project, not your entire vault.

Add `zotero_key` to your plugin template (e.g. `templates/zoterosummary.md`):

```yaml
---
citekey: {{citekey}}
zotero_key: {{key}}
---
```

Existing notes without the field can have it added manually.

### Citekeys in refs.bib

When a note is linked, `zobs` reads the `citekey` field from its frontmatter
and uses that as the citekey in `refs.bib`. Without Obsidian, the raw 8-char
Zotero key is used instead (e.g. `AB12CD34`).

---

## Design

PDFs and notes are symlinked, not copied. Zotero owns the PDFs, Obsidian owns
the notes and this connects those to your writing/experiment workspace. Symlinks are machine-local (gitignored), so each machine runs `get-papers` once to materialise them. The only committed output is `references/refs.bib`.

---

## New project pipeline

1. `uv init my-project && cd my-project`
2. `uv add zobs`
3. Copy `.env.example`, fill in credentials
4. Add `references/papers/`, `references/notes/`, `references/.zobs-cache/`, `.env` to `.gitignore`
5. `uv run zobs`
