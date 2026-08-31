"""Command-line entry point for zobs.

``zobs`` with no subcommand runs the sync (unchanged). ``zobs augment`` fills
in incomplete refs.bib entries from bibliographic APIs.
"""

from __future__ import annotations

import argparse


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zobs",
        description="Sync a Zotero selection and Obsidian notes into a research project.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("sync", help="Sync PDFs, notes and refs.bib (this is the default).")

    aug = sub.add_parser(
        "augment",
        help="Fill in incomplete refs.bib entries from bibliographic APIs.",
    )
    target = aug.add_mutually_exclusive_group()
    target.add_argument(
        "--select",
        metavar="KEYS",
        help="comma-separated Zotero keys or citekeys to augment",
    )
    target.add_argument(
        "--all",
        action="store_true",
        help="consider every entry, not just the incomplete ones",
    )
    aug.add_argument(
        "--include-complete",
        action="store_true",
        help="show complete entries in the picker too (left unchecked)",
    )
    aug.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="print candidate entries as JSON and exit (no network calls)",
    )
    aug.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve metadata and show the diff, but write nothing",
    )
    aug.add_argument(
        "-y", "--yes", action="store_true", help="skip the confirmation prompt"
    )
    aug.add_argument(
        "--no-zotero",
        action="store_true",
        help="update refs.bib only, do not push changes back to Zotero",
    )
    aug.add_argument(
        "--no-bib",
        action="store_true",
        help="push to Zotero only, do not touch refs.bib",
    )
    aug.add_argument(
        "--overwrite",
        action="store_true",
        help="replace existing non-empty fields too (default: fill blanks only)",
    )
    aug.add_argument(
        "--no-repair",
        action="store_true",
        help="keep malformed values such as year='June' instead of refilling them",
    )
    aug.add_argument(
        "--min-confidence",
        type=float,
        default=93.0,
        metavar="RATIO",
        help="token_set_ratio bar (0-100) for matches without a DOI (default: 93)",
    )
    aug.add_argument(
        "--sources",
        default="crossref,datacite,dblp,openalex,arxiv",
        help="comma-separated source list / order",
    )
    aug.add_argument(
        "--refresh", action="store_true", help="ignore the local API response cache"
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    if args.command == "augment":
        from zobs.augment import run_augment

        raise SystemExit(run_augment(args))

    from zobs.sync import main as sync_main

    sync_main()
