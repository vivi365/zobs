"""
zobs — Zotero + Obsidian sync for research projects.

Syncs PDFs and a .bib file from a Zotero collection, and links matching
Obsidian notes into references/notes/ using the Better BibTeX cite key
stored in each note's frontmatter.

Configuration (via .env in the project root):
    ZOTERO_USER_ID    — numeric Zotero user ID
    ZOTERO_API_KEY    — Zotero API key
    ZOTERO_SYNC_MODE  — selection mode: collection (default) or tag
    ZOTERO_COLLECTION — collection name or 8-char key (mode=collection)
    ZOTERO_TAG        — tag name or comma-separated list (mode=tag)
    ZOTERO_STORAGE    — path to Zotero storage dir (default: ~/Zotero/storage)
    ZOTERO_BBT_URL    — Better BibTeX local export URL (optional)
    OBSIDIAN_NOTES    — path to Obsidian paper-summaries folder (optional)
"""

import os
import sys
import urllib.error
import urllib.request
import yaml
from pathlib import Path
from dotenv import load_dotenv
from pyzotero import zotero, errors as zotero_errors

from zobs.selector import SelectorError, ZoteroClient, parse_selector

# Config

ITEM_TYPES = "journalArticle || conferencePaper || preprint || report || webpage"


def load_config() -> dict:
    """Load and validate configuration from .env in the current working directory."""
    load_dotenv(Path.cwd() / ".env")

    required = ("ZOTERO_USER_ID", "ZOTERO_API_KEY")
    raw_values = {k: os.environ.get(k) for k in required}
    missing = []
    for k, v in raw_values.items():
        if not v or not v.strip():
            missing.append(k)
            continue
        if v.lstrip().startswith("#"):
            missing.append(k)
    if missing:
        print(f"[error] Missing required .env variables: {', '.join(missing)}")
        print("        Copy .env.example to .env and fill in your credentials.")
        sys.exit(1)

    try:
        selector = parse_selector(os.environ)
    except SelectorError as e:
        print(f"[error] {e}")
        print("        Copy .env.example to .env and fill in your credentials.")
        sys.exit(1)

    obsidian_raw = os.environ.get("OBSIDIAN_NOTES")
    obsidian_raw = obsidian_raw.strip() if obsidian_raw else None
    bbt_url_raw = os.environ.get("ZOTERO_BBT_URL")
    bbt_url_raw = bbt_url_raw.strip() if bbt_url_raw else None
    return {
        "user_id": raw_values["ZOTERO_USER_ID"].strip(),
        "api_key": raw_values["ZOTERO_API_KEY"].strip(),
        "selector": selector,
        "storage": Path(
            os.environ.get("ZOTERO_STORAGE", Path.home() / "Zotero" / "storage")
        ),
        "obsidian": Path(obsidian_raw) if obsidian_raw else None,
        "bbt_url": bbt_url_raw,
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

        try:
            fm = parse_frontmatter(text)
        except yaml.YAMLError:
            print(f"  [warn] invalid frontmatter, skipped: {note}")
            continue
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


def fetch_bbt_bib(url: str) -> str:
    """Fetch a Better BibTeX export from the local HTTP endpoint."""
    req = urllib.request.Request(url, headers={"User-Agent": "zobs/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset)
    except urllib.error.URLError as e:
        raise RuntimeError(f"Better BibTeX export failed: {e}") from e


def build_bib_entry(item: dict, cite_key: str) -> str:
    """Build a minimal BibTeX entry from a Zotero item."""
    data = item["data"]
    authors = data.get("creators", [])

    def fmt_author(a: dict) -> str:
        last = a.get("lastName", "")
        first = a.get("firstName", "")
        if first:
            return f"{last}, {first}"
        return f"{{{last}}}"

    author_str = (
        " and ".join(fmt_author(a) for a in authors if a.get("creatorType") == "author")
        or "Unknown"
    )
    year = (data.get("date") or "")[:4]
    item_type = (data.get("itemType") or "").strip()
    title = data.get("title", "")
    doi = data.get("DOI", "")

    if item_type == "conferencePaper":
        booktitle = (
            data.get("proceedingsTitle")
            or data.get("conferenceName")
            or data.get("publicationTitle")
            or ""
        )
        return (
            f"@inproceedings{{{cite_key},\n"
            f"  title     = {{{title}}},\n"
            f"  author    = {{{author_str}}},\n"
            f"  booktitle = {{{booktitle}}},\n"
            f"  year      = {{{year}}},\n"
            f"  doi       = {{{doi}}},\n"
            f"}}\n"
        )

    return (
        f"@article{{{cite_key},\n"
        f"  title   = {{{title}}},\n"
        f"  author  = {{{author_str}}},\n"
        f"  year    = {{{year}}},\n"
        f"  journal = {{{data.get('publicationTitle', '')}}},\n"
        f"  doi     = {{{doi}}},\n"
        f"}}\n"
    )


def citation_key_from_item(data: dict, zotero_key: str) -> str:
    """Prefer Better BibTeX citation key when present; fallback to Zotero key."""
    cite_key = data.get("citationKey")
    if cite_key:
        return str(cite_key)
    extra = str(data.get("extra", ""))
    for line in extra.splitlines():
        if ":" not in line:
            continue
        label, value = line.split(":", 1)
        if label.strip().lower() == "citation key" and value.strip():
            return value.strip()
    return zotero_key


# Main


def main() -> None:
    cfg = load_config()

    repo_root = Path.cwd()
    papers_dir = repo_root / "references" / "papers"
    notes_dir = repo_root / "references" / "notes"
    bib_file = repo_root / "references" / "refs.bib"

    papers_dir.mkdir(parents=True, exist_ok=True)
    notes_dir.mkdir(parents=True, exist_ok=True)
    obsidian_dir = notes_dir / "obsidian"

    # Obsidian index
    obsidian_index: dict[str, tuple[Path, str]] = {}
    if cfg["obsidian"]:
        obsidian_dir.mkdir(parents=True, exist_ok=True)
        print("Scanning Obsidian notes...")
        obsidian_index = scan_obsidian_notes(cfg["obsidian"])
        print(f"  Found {len(obsidian_index)} notes with zotero_key.\n")

    # Zotero sync
    zot: ZoteroClient = zotero.Zotero(cfg["user_id"], "user", cfg["api_key"])
    try:
        items = cfg["selector"].fetch_items(zot, ITEM_TYPES)
    except ValueError as e:
        print(f"[error] {e}")
        sys.exit(1)
    except RuntimeError as e:
        print(f"[error] {e}")
        sys.exit(1)
    except zotero_errors.HTTPError as e:
        print("[error] Zotero API error while fetching items.")
        print(f"        {e}")
        sys.exit(1)

    bib_entries: list[str] = []
    synced, migrated, skipped = 0, 0, 0
    notes_linked, notes_unlinked, notes_missing = 0, 0, 0
    expected_pdfs: set[str] = set()
    expected_zotero_notes: set[str] = set()
    selection_obsidian_targets: set[Path] = set()

    for item in items:
        data = item["data"]
        title = data.get("title", "untitled")
        zotero_key = data.get("key")

        note_path, note_cite_key = obsidian_index.get(zotero_key, (None, None))
        cite_key = note_cite_key or citation_key_from_item(data, zotero_key)
        if note_path:
            selection_obsidian_targets.add(note_path.resolve())

        slug_title = slugify(title)
        dest = papers_dir / f"{cite_key}_{slug_title}.pdf"
        old_dest = papers_dir / f"{zotero_key}_{slug_title}.pdf"
        expected_pdfs.add(dest.name)
        expected_zotero_notes.add(f"{cite_key}.md")

        # ── PDF ───────────────────────────────────────────────────────────────
        try:
            attachments = zot.children(zotero_key, itemType="attachment")
        except Exception as e:
            print(f"  [err]  API error for {zotero_key}: {e}")
            if not cfg["bbt_url"]:
                bib_entries.append(build_bib_entry(item, cite_key))
            continue

        def is_pdf_attachment(att: dict) -> bool:
            data = att.get("data", {})
            content_type = str(data.get("contentType", "")).lower()
            filename = str(data.get("filename", "")).lower()
            return content_type in {
                "application/pdf",
                "application/x-pdf",
            } or filename.endswith(".pdf")

        pdf = next((a for a in attachments if is_pdf_attachment(a)), None)

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
        note_dest = obsidian_dir / f"{cite_key}.md"
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

        if not cfg["bbt_url"]:
            bib_entries.append(build_bib_entry(item, cite_key))

    # Remove PDFs and Zotero notes no longer in the selection
    pdfs_unlinked = 0
    for link in papers_dir.iterdir():
        if link.is_symlink() and link.name not in expected_pdfs:
            link.unlink()
            print(f"  [unlink] {link.name} (not in selection)")
            pdfs_unlinked += 1

    zotero_notes_dir = notes_dir / "zotero"
    if zotero_notes_dir.exists():
        for f in zotero_notes_dir.iterdir():
            if f.name not in expected_zotero_notes:
                f.unlink()
                print(f"  [unlink] {f.name} (not in selection)")
                notes_unlinked += 1

    # Remove Obsidian symlinks not belonging to the current selection
    if cfg["obsidian"]:
        for link in obsidian_dir.iterdir():
            if not link.is_symlink():
                continue
            if not link.exists() or link.resolve() not in selection_obsidian_targets:
                link.unlink()
                print(f"  [unlink] {link.name} (not in selection)")
                notes_unlinked += 1

    if cfg["bbt_url"]:
        bib_text = fetch_bbt_bib(cfg["bbt_url"])
        bib_file.write_text(bib_text)
        bib_summary = "refs.bib updated (Better BibTeX export)."
    else:
        bib_file.write_text("\n".join(bib_entries))
        bib_summary = f"refs.bib updated ({len(bib_entries)} entries)."

    notes_summary = (
        f", {notes_linked} notes linked, {notes_unlinked} unlinked, {notes_missing} no note"
    )
    print(
        f"\nDone: {synced} new, {migrated} migrated, {skipped} skipped, {pdfs_unlinked} PDFs removed{notes_summary}. {bib_summary}"
    )
