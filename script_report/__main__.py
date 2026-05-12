"""script.report CLI dispatcher.

Usage:
    python -m script_report build         # rebuild site_data.js
    python -m script_report prerender     # emit static SEO pages (/drug/<slug>/, /sponsor/<key>/)
    python -m script_report refresh       # full pipeline (download → extract → build → deploy)
    python -m script_report download      # download new PBAC PSDs
    python -m script_report extract       # Haiku field extraction
    python -m script_report sponsor       # backfill sponsor column on existing rows
    python -m script_report spend         # fetch PBS drug spend Excel
    python -m script_report schedule      # backfill ATC codes from PBS Schedule
    python -m script_report atc           # parse ATC class data
    python -m script_report calendar      # parse PBS Cycle Timeframe PDFs
    python -m script_report agendas       # extract upcoming PBAC + Intracycle meeting agendas
    python -m script_report embed         # build Voyage embeddings + nearest table
    python -m script_report brandmap      # build data/brand_to_generic.json from PBS Schedule + PSDs

Each subcommand passes any remaining argv on to the underlying entry function,
so flags like --resume / --no-deploy work as before.
"""

from __future__ import annotations

import sys


_COMMANDS = {
    "build":    ("script_report.builders.site_builder", "main"),
    "prerender":("script_report.builders.prerenderer",  "main"),
    "refresh":  ("script_report.refresh",               "main"),
    "download": ("script_report.scrapers.psd_downloader",   "main"),
    "extract":  ("script_report.extractors.psd_extractor",  "main"),
    "sponsor":  ("script_report.extractors.sponsor_backfill","main"),
    "spend":    ("script_report.scrapers.pbs_spend",        "main"),
    "schedule": ("script_report.scrapers.pbs_schedule",     "main"),
    "atc":      ("script_report.parsers.atc_parser",        "main"),
    "calendar": ("script_report.parsers.pbac_calendar",     "main"),
    "agendas":  ("script_report.extractors.agenda_extractor","main"),
    "embed":    ("script_report.embedders.voyage_embedder", "main"),
    "brandmap": ("script_report.builders.brand_map_builder", "main"),
}


def _print_help() -> None:
    print(__doc__)
    print("Commands:")
    for name in _COMMANDS:
        print(f"  {name}")


def main() -> None:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        _print_help()
        return

    cmd, *rest = argv
    if cmd not in _COMMANDS:
        print(f"Unknown command: {cmd}\n")
        _print_help()
        sys.exit(2)

    module_name, func_name = _COMMANDS[cmd]
    # Rewrite argv so the underlying main() sees its own argparse args
    sys.argv = [f"{module_name}", *rest]
    import importlib
    mod = importlib.import_module(module_name)
    fn = getattr(mod, func_name)
    fn()


if __name__ == "__main__":
    main()
