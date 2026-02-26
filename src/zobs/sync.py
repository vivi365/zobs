"""
zobs — Zotero + Obsidian sync for research projects.

Syncs PDFs and a .bib file from a Zotero collection, and links matching
Obsidian notes into references/notes/ using the Better BibTeX cite key
stored in each note's frontmatter.

Configuration (via .env in the project root):
    ZOTERO_USER_ID    — numeric Zotero user ID
    ZOTERO_API_KEY    — Zotero API key
    ZOTERO_COLLECTION — collection name or 8-char key
    ZOTERO_STORAGE    — path to Zotero storage dir (default: ~/Zotero/storage)
    OBSIDIAN_NOTES    — path to Obsidian paper-summaries folder (optional)
"""

import os
import sys
import yaml
from pathlib import Path
from dotenv import load_dotenv
from pyzotero import zotero

# Config

ITEM_TYPES = "journalArticle || conferencePaper || preprint || report"


def load_config() -> dict:
    """Load and validate configuration from .env in the current working directory."""
    load_dotenv(Path.cwd() / ".env")

    missing = [
        k
        for k in ("ZOTERO_USER_ID", "ZOTERO_API_KEY", "ZOTERO_COLLECTION")
        if not os.environ.get(k)
    ]
    if missing:
        print(f"[error] Missing required .env variables: {', '.join(missing)}")
        print("        Copy .env.example to .env and fill in your credentials.")
        sys.exit(1)

    obsidian_raw = os.environ.get("OBSIDIAN_NOTES")
    return {
        "user_id": os.environ["ZOTERO_USER_ID"],
        "api_key": os.environ["ZOTERO_API_KEY"],
        "collection": os.environ["ZOTERO_COLLECTION"],
        "storage": Path(
            os.environ.get("ZOTERO_STORAGE", Path.home() / "Zotero" / "storage")
        ),
        "obsidian": Path(obsidian_raw) if obsidian_raw else None,
    }


# Helpers


def slugify(title: str) -> str:
    """Make a filename-safe slug from a title."""
    return (
        "".join(c if c.isalnum() or c in " -_" else "" for c in title)
        .strip()
        .replace(" ", "_")[:80]
    )


def parse_frontmatter(text: str) -> dict:
    """Parse YAML frontmatter from a markdown file."""
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    return yaml.safe_load(parts[1]) or {}


def scan_obsidian_notes(notes_root: Path) -> dict[str, tuple[Path, str]]:
    """
    Scan all .md files under notes_root and return a map:
        zotero_key -> (note_path, cite_key)

    Only indexes notes that have both a citekey and a zotero_key in
    frontmatter — the user explicitly opts a note in by adding zotero_key.
    Aborts if the path is inaccessible to prevent bib corruption.
    """
    if not notes_root.exists():
        print(f"[error] OBSIDIAN_NOTES not accessible: {notes_root}")
        print(
            "        Grant Full Disk Access to Terminal in System Settings → Privacy & Security."
        )
        sys.exit(1)

    index: dict[str, tuple[Path, str]] = {}
    skipped = 0

    for note in notes_root.rglob("*.md"):
        try:
            text = note.read_text(encoding="utf-8")
        except OSError:
            skipped += 1
            continue

        fm = parse_frontmatter(text)
        cite_key = fm.get("citekey")
        zotero_key = fm.get("zotero_key")
        if cite_key and zotero_key:
            index[str(zotero_key)] = (note, str(cite_key))

    if skipped:
        print(f"  [warn] {skipped} notes unreadable (permissions?)")

    return index


def html_to_text(html: str) -> str:
    """Strip HTML tags for basic Zotero note content."""
    import re

    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def fetch_zotero_note(zot: zotero.Zotero, zotero_key: str, cite_key: str) -> str | None:
    """
    Fetch the first child note for a Zotero item and return it as markdown.
    Returns None if no note exists.
    """
    try:
        notes = zot.children(zotero_key, itemType="note")
    except Exception:
        return None
    if not notes:
        return None
    content = notes[0]["data"].get("note", "").strip()
    if not content:
        return None
    return f"# {cite_key}\n\n*Imported from Zotero*\n\n{html_to_text(content)}\n"


def build_bib_entry(item: dict, cite_key: str) -> str:
    """Build a minimal BibTeX entry from a Zotero item."""
    data = item["data"]
    authors = data.get("creators", [])
    author_str = (
        " and ".join(
            f"{a.get('lastName', '')}, {a.get('firstName', '')}"
            for a in authors
            if a.get("creatorType") == "author"
        )
        or "Unknown"
    )
    year = (data.get("date") or "")[:4]
    return (
        f"@article{{{cite_key},\n"
        f"  title   = {{{data.get('title', '')}}},\n"
        f"  author  = {{{author_str}}},\n"
        f"  year    = {{{year}}},\n"
        f"  journal = {{{data.get('publicationTitle', '')}}},\n"
        f"  doi     = {{{data.get('DOI', '')}}},\n"
        f"}}\n"
    )


def resolve_collection_key(zot: zotero.Zotero, name_or_key: str) -> str:
    """Accept either a collection name or 8-char key; return the key."""
    if len(name_or_key) == 8 and name_or_key.isalnum():
        return name_or_key
    collections = zot.collections()
    matches = [
        c for c in collections if c["data"]["name"].lower() == name_or_key.lower()
    ]
    if not matches:
        available = [c["data"]["name"] for c in collections]
        raise ValueError(
            f"Collection '{name_or_key}' not found. Available: {available}"
        )
    if len(matches) > 1:
        raise ValueError(
            f"Multiple collections named '{name_or_key}'. Use the 8-char key instead."
        )
    return matches[0]["data"]["key"]


# Main


def main() -> None:
    cfg = load_config()

    repo_root = Path.cwd()
    papers_dir = repo_root / "references" / "papers"
    notes_dir = repo_root / "references" / "notes"
    bib_file = repo_root / "references" / "refs.bib"

    papers_dir.mkdir(parents=True, exist_ok=True)

    # Obsidian index
    obsidian_index: dict[str, tuple[Path, str]] = {}
    if cfg["obsidian"]:
        (notes_dir / "obsidian").mkdir(parents=True, exist_ok=True)
        print("Scanning Obsidian notes...")
        obsidian_index = scan_obsidian_notes(cfg["obsidian"])
        print(f"  Found {len(obsidian_index)} notes with zotero_key.\n")

    # Zotero sync
    zot = zotero.Zotero(cfg["user_id"], "user", cfg["api_key"])
    collection_key = resolve_collection_key(zot, cfg["collection"])
    items = zot.collection_items(collection_key, itemType=ITEM_TYPES)

    bib_entries = []
    synced, migrated, skipped = 0, 0, 0
    notes_linked, notes_missing = 0, 0

    for item in items:
        data = item["data"]
        title = data.get("title", "untitled")
        zotero_key = data.get("key")

        note_path, cite_key = obsidian_index.get(zotero_key, (None, zotero_key))

        slug_title = slugify(title)
        dest = papers_dir / f"{cite_key}_{slug_title}.pdf"
        old_dest = papers_dir / f"{zotero_key}_{slug_title}.pdf"

        # ── PDF ───────────────────────────────────────────────────────────────
        try:
            attachments = zot.children(zotero_key, itemType="attachment")
        except Exception as e:
            print(f"  [err]  API error for {zotero_key}: {e}")
            bib_entries.append(build_bib_entry(item, cite_key))
            continue

        pdf = next(
            (
                a
                for a in attachments
                if a["data"].get("contentType") == "application/pdf"
            ),
            None,
        )

        if pdf is None:
            print(f"  [skip] no PDF: {title[:60]}")
            skipped += 1
        elif dest.exists() or dest.is_symlink():
            print(f"  [ok]   {dest.name}")
            skipped += 1
        elif old_dest.exists() or old_dest.is_symlink():
            old_dest.rename(dest)
            print(f"  [migr] {old_dest.name} -> {dest.name}")
            migrated += 1
        else:
            att_key = pdf["data"]["key"]
            local_dir = cfg["storage"] / att_key
            pdfs = list(local_dir.glob("*.pdf")) if local_dir.exists() else []
            if pdfs:
                dest.symlink_to(pdfs[0])
                print(f"  [link] {dest.name}")
                synced += 1
            else:
                print(f"  [skip] not local yet: {title[:60]}")
                skipped += 1

        # Note
        notes_dir.mkdir(parents=True, exist_ok=True)
        note_dest = notes_dir / "obsidian" / f"{cite_key}.md"
        if note_dest.exists() or note_dest.is_symlink():
            pass  # already linked
        elif note_path:
            note_dest.symlink_to(note_path)
            print(f"  [note] {cite_key}.md (obsidian)")
            notes_linked += 1
        else:
            # Fallback: fetch note written directly in Zotero
            zotero_note = fetch_zotero_note(zot, zotero_key, cite_key)
            if zotero_note:
                (notes_dir / "zotero").mkdir(parents=True, exist_ok=True)
                note_dest = notes_dir / "zotero" / f"{cite_key}.md"
                note_dest.write_text(zotero_note)
                print(f"  [note] {cite_key}.md (zotero)")
                notes_linked += 1
            else:
                notes_missing += 1

        bib_entries.append(build_bib_entry(item, cite_key))

    # Link notes with zotero_key not matched to any collection item
    if cfg["obsidian"]:
        obsidian_dir = notes_dir / "obsidian"
        already_linked = {p.resolve() for p in obsidian_dir.iterdir() if p.is_symlink()}
        for zk, (note_path, cite_key) in obsidian_index.items():
            if note_path.resolve() in already_linked:
                continue
            note_dest = obsidian_dir / f"{cite_key}.md"
            if not (note_dest.exists() or note_dest.is_symlink()):
                note_dest.symlink_to(note_path)
                print(f"  [note] {cite_key}.md (zotero_key={zk}, not in collection)")
                notes_linked += 1

    bib_file.write_text("\n".join(bib_entries))

    notes_summary = f", {notes_linked} notes linked, {notes_missing} no note"
    print(
        f"\nDone: {synced} new, {migrated} migrated, {skipped} skipped{notes_summary}. refs.bib updated ({len(bib_entries)} entries)."
    )
