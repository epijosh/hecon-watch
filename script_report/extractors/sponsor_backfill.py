"""
sponsor_backfill.py
━━━━━━━━━━━━━━━━━━━
Backfills the `sponsor` column on existing rows of psd_extracted.csv.

Why this exists separately from psd_extractor.py:
  Re-running the full extractor across ~2,000 PSDs to pick up one new field
  is wasteful — every row already has 25 other fields. This module reads
  page 1 only, asks Haiku for just the sponsor, and writes that one cell.

Cost: ~$0.0008 per PSD with Haiku → ~$2 for the whole corpus.

USAGE:
  python -m script_report sponsor              # backfill any rows where sponsor is empty
  python -m script_report sponsor --limit 20   # cheap dry-run
  python -m script_report sponsor --force      # re-extract every row (overwrites existing values)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

from script_report.config import DATA_DIR, HAIKU_MODEL, REPO_ROOT
from script_report.extractors.psd_extractor import (
    FIELDS,
    extract_html_text,
    extract_pdf_text,
)
from script_report.utils.helpers import load_dotenv_safely

load_dotenv_safely()

try:
    import anthropic
except ImportError:
    print("Missing dependency. Run: pip install anthropic --break-system-packages")
    sys.exit(1)


SYSTEM_PROMPT = (
    "You extract the sponsor company from PBAC Public Summary Documents. "
    "Return only the JSON object requested."
)

USER_PROMPT_TEMPLATE = """The text below is the first page of a PBAC Public Summary Document. Find the SPONSOR — the company that lodged the submission. It is usually labelled "Sponsor:", "Sponsor's name:", or appears under the drug/indication block on the title page.

Return ONLY this JSON object (no markdown, no commentary):

{{"sponsor": "<company name, lightly normalised — drop trailing 'Pty Ltd', 'Limited', 'Australia', 'Pharmaceuticals' suffixes only when at the very end. Examples: 'Roche Products', 'Merck Sharp & Dohme', 'Novartis Pharmaceuticals'. Use null if not stated.>"}}

TEXT:
{text}"""


def _psd_dirs() -> list[Path]:
    dirs = []
    if (DATA_DIR / "psds").exists():
        dirs.append(DATA_DIR / "psds")
    dirs.append(REPO_ROOT)
    return dirs


def _find_psd(filename: str, search_dirs: list[Path]) -> Path | None:
    for d in search_dirs:
        p = d / filename
        if p.exists():
            return p
    return None


def _short_text(path: Path) -> str:
    """First page of PDFs / first ~3k chars of HTML — enough for the title block."""
    if path.suffix.lower() == ".pdf":
        return extract_pdf_text(path, max_pages=1)[:3000]
    if path.suffix.lower() == ".html":
        return extract_html_text(path)[:3000]
    return ""


def _call(client: "anthropic.Anthropic", text: str) -> str | None:
    if len(text.strip()) < 60:
        return None
    prompt = USER_PROMPT_TEMPLATE.format(text=text)
    retries = 0
    while retries <= 3:
        try:
            msg = client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=120,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            data = json.loads(raw)
            if isinstance(data, dict):
                v = data.get("sponsor")
                if v and isinstance(v, str):
                    return v.strip()
            return None
        except anthropic.RateLimitError:
            wait = min(60, 5 * (2 ** retries))
            print(f" [rate limit — waiting {wait}s]", end="", flush=True)
            time.sleep(wait)
            retries += 1
        except (json.JSONDecodeError, anthropic.APIError):
            return None
    return None


def _read_rows(csv_path: Path) -> tuple[list[str], list[dict]]:
    if not csv_path.exists():
        print(f"ERROR: {csv_path} not found — run `python -m script_report extract` first.")
        sys.exit(1)
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    return fieldnames, rows


def _write_rows(csv_path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    if "sponsor" not in fieldnames:
        # Insert sponsor right before pbac_year so column order matches FIELDS
        try:
            idx = fieldnames.index("pbac_year")
        except ValueError:
            idx = len(fieldnames)
        fieldnames = fieldnames[:idx] + ["sponsor"] + fieldnames[idx:]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill sponsor column on psd_extracted.csv")
    parser.add_argument("--limit", type=int, default=0, help="Process only first N rows that need work")
    parser.add_argument("--delay", type=float, default=0.4, help="Seconds between API calls")
    parser.add_argument("--force", action="store_true", help="Re-extract sponsor even when already filled")
    parser.add_argument("--flush-every", type=int, default=25, help="Write CSV back to disk every N rows")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key.startswith("sk-"):
        print("ERROR: ANTHROPIC_API_KEY not set — see .env")
        sys.exit(1)
    client = anthropic.Anthropic(api_key=api_key)

    csv_path = DATA_DIR / "psd_extracted.csv"
    fieldnames, rows = _read_rows(csv_path)
    psd_dirs = _psd_dirs()

    needs = []
    for row in rows:
        if row.get("extraction_ok") != "yes":
            continue
        if not args.force and (row.get("sponsor") or "").strip():
            continue
        needs.append(row)

    if args.limit:
        needs = needs[: args.limit]

    print("=" * 62)
    print("Sponsor backfill")
    print("=" * 62)
    print(f"  CSV rows         : {len(rows):,}")
    print(f"  To backfill      : {len(needs):,}")
    print(f"  Est. cost        : ~${len(needs) * 0.0008:.2f}")
    print()

    if not needs:
        print("Nothing to do. Use --force to re-extract.")
        # Still write the file to add the column header for downstream callers.
        _write_rows(csv_path, fieldnames, rows)
        return

    ok = miss = 0
    last_flush = 0
    for i, row in enumerate(needs, 1):
        fname = (row.get("filename") or "").strip()
        path = _find_psd(fname, psd_dirs)
        short = fname[:48]
        print(f"  [{i:4d}/{len(needs)}] {short:<48}", end="", flush=True)

        if not path:
            miss += 1
            print("  [--]  PSD file not found")
            continue

        text = _short_text(path)
        sponsor = _call(client, text)
        if sponsor:
            row["sponsor"] = sponsor
            ok += 1
            print(f"  [OK]  {sponsor[:40]}")
        else:
            miss += 1
            print("  [--]  no sponsor parsed")

        if (i - last_flush) >= args.flush_every:
            _write_rows(csv_path, fieldnames, rows)
            last_flush = i

        time.sleep(args.delay)

    _write_rows(csv_path, fieldnames, rows)
    print()
    print(f"  Backfilled  [OK] {ok:,}   Missed [--] {miss:,}")
    print(f"  Wrote       {csv_path.name}")
    print()
    print("Next step: python -m script_report build")


if __name__ == "__main__":
    main()
