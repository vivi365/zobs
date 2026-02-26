# zobs

Syncs a specific Zotero collection to your local project with optional Obsidian notes syncing.

- Symlinks PDFs from local Zotero storage into `references/papers/`
- Generates `references/refs.bib`
- Links paper notes into `references/notes/` (from Obsidian, or Zotero's built-in notes as fallback)

## Setup and Usage

```bash
uv add zobs
```

Use the `.env.example` to create your `.env` config


| Variable | Required | Description |
|---|---|---|
| `ZOTERO_USER_ID` | yes | Numeric Zotero user ID (visible in your Zotero profile URL) |
| `ZOTERO_API_KEY` | yes | Create at zotero.org/settings/keys |
| `ZOTERO_COLLECTION` | yes | Collection name or 8-char key to sync |
| `ZOTERO_STORAGE` | no | Path to Zotero local storage (default: `~/Zotero/storage`) |
| `OBSIDIAN_NOTES` | no | Path to your Obsidian paper-summaries folder — see below |

> API key and user ID are retrieved from [zotero.org/settings/keys](https://www.zotero.org/settings/keys). The collection id can be seen in the last part of URL in the Zotero web interface, e.g. `https://www.zotero.org/<username>/collections/<collection-id>/collection`

Sync pdfs and notes:
```bash
uv run zobs
```

---

## Obsidian integration (optional)

Without `OBSIDIAN_NOTES`, the package still works: PDFs sync, `refs.bib` is
generated using raw 8-char Zotero keys as citekeys, and any notes written
directly in Zotero (child notes on items) are exported to `references/notes/zotero/`.

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
4. Add `references/papers/`, `references/notes/`, `.env` to `.gitignore`
5. `uv run zobs`
