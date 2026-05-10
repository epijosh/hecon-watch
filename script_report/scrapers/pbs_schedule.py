"""
script_report.scrapers.pbs_schedule
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Downloads the monthly PBS Schedule "PBS API CSV files" bundle from
pbs.gov.au, extracts the two tables we need (items + item-atc-relationships),
and writes a flat ``data/pbs_schedule_atc.csv`` mapping
``drug_name (lowercase) → atc_code``.

This file is consumed by the Voyage embedder's ATC tiebreaker so similar-drug
ranking can disambiguate near-tied cosine candidates by ATC prefix even when
the source / candidate isn't in the spend top-list.

Output schema (data/pbs_schedule_atc.csv):
    drug_name      lowercased generic name as published in the schedule
    atc_code       e.g. "L01EC02" (full substance-level ATC)
    brand_name     a representative brand if available, else ""
    pbs_code       sample PBS item code (one of possibly many for this drug)

USAGE:
    python -m script_report schedule          # latest month's bundle
    python -m script_report schedule --month 2026-04  # a specific month
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import zipfile
from datetime import date
from pathlib import Path

from script_report.config import DATA_DIR


BUNDLE_URL_TEMPLATE = (
    "https://www.pbs.gov.au/downloads/{year}/{month:02d}/"
    "{year}-{month:02d}-01-PBS-API-CSV-files.zip"
)
ITEMS_TABLE = "tables_as_csv/items.csv"
ATC_REL_TABLE = "tables_as_csv/item-atc-relationships.csv"
OUTPUT_CSV = DATA_DIR / "pbs_schedule_atc.csv"

USER_AGENT = "Mozilla/5.0 (compatible; script.report PBS Schedule fetcher; +https://script.report)"


def _bundle_url(month: str | None) -> str:
    """Resolve YYYY-MM into the bundle URL. Defaults to today's month."""
    if month:
        try:
            y, m = month.split("-")
            year, mon = int(y), int(m)
        except (ValueError, AttributeError) as e:
            raise SystemExit(f"--month must be YYYY-MM (got {month!r}): {e}")
    else:
        today = date.today()
        year, mon = today.year, today.month
    return BUNDLE_URL_TEMPLATE.format(year=year, month=mon)


def _download(url: str) -> bytes:
    try:
        import requests
    except ImportError:
        print("Missing requests. Run: pip install -r requirements.txt --break-system-packages")
        sys.exit(1)

    print(f"  Downloading: {url}")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/zip,*/*"}
    r = requests.get(url, headers=headers, timeout=120)
    if r.status_code == 404:
        raise SystemExit(
            f"Bundle not found at {url}\n"
            "  Try --month YYYY-MM with the previous month, or check\n"
            "  https://www.pbs.gov.au/info/browse/download for the current URL."
        )
    r.raise_for_status()
    print(f"  Downloaded {len(r.content):,} bytes")
    return r.content


def _build_drug_atc_map(bundle_bytes: bytes) -> list[dict]:
    """Read the two relevant CSVs from the in-memory ZIP and join them.

    Returns a list of {drug_name, atc_code, brand_name, pbs_code} dicts —
    one per unique drug_name (lowercased), preferring the highest
    atc_priority_pct row per item and the first-seen brand per drug.
    """
    with zipfile.ZipFile(io.BytesIO(bundle_bytes)) as z:
        members = set(z.namelist())
        if ITEMS_TABLE not in members or ATC_REL_TABLE not in members:
            raise SystemExit(
                f"Bundle is missing expected tables. Got: {sorted(members)[:8]}…"
            )
        items_text   = z.read(ITEMS_TABLE).decode("utf-8")
        atc_rel_text = z.read(ATC_REL_TABLE).decode("utf-8")

    # ── Build pbs_code → (atc_code, priority) ────────────────────────────────
    atc_by_pbs_code: dict[str, tuple[str, int]] = {}
    for r in csv.DictReader(io.StringIO(atc_rel_text)):
        pbs_code = (r.get("pbs_code") or "").strip()
        atc_code = (r.get("atc_code") or "").strip()
        if not pbs_code or not atc_code:
            continue
        try:
            prio = int(r.get("atc_priority_pct") or 0)
        except (ValueError, TypeError):
            prio = 0
        existing = atc_by_pbs_code.get(pbs_code)
        if existing is None or prio > existing[1]:
            atc_by_pbs_code[pbs_code] = (atc_code, prio)

    # ── Walk items, attach ATC, dedupe by drug_name ──────────────────────────
    drug_to_row: dict[str, dict] = {}
    items_without_atc = 0
    for r in csv.DictReader(io.StringIO(items_text)):
        pbs_code = (r.get("pbs_code") or "").strip()
        drug_name = (r.get("drug_name") or "").strip()
        brand_name = (r.get("brand_name") or "").strip()
        if not pbs_code or not drug_name:
            continue
        atc_pair = atc_by_pbs_code.get(pbs_code)
        if not atc_pair:
            items_without_atc += 1
            continue
        atc_code, _ = atc_pair
        key = drug_name.lower()
        if key not in drug_to_row:
            drug_to_row[key] = {
                "drug_name": key,
                "atc_code":  atc_code,
                "brand_name": brand_name,
                "pbs_code":   pbs_code,
            }

    print(f"  Items with ATC : {sum(1 for _ in atc_by_pbs_code):,}")
    print(f"  Items skipped  : {items_without_atc:,}  (no ATC link)")
    print(f"  Unique drugs   : {len(drug_to_row):,}")
    return sorted(drug_to_row.values(), key=lambda r: r["drug_name"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill ATC codes from the PBS Schedule")
    parser.add_argument("--month", default=None, help="YYYY-MM (default: current month)")
    args = parser.parse_args()

    print("=" * 62)
    print("PBS Schedule — ATC backfill")
    print("=" * 62)

    url = _bundle_url(args.month)
    blob = _download(url)
    rows = _build_drug_atc_map(blob)

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["drug_name", "atc_code", "brand_name", "pbs_code"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Wrote → {OUTPUT_CSV}  ({len(rows):,} drugs)")
    print()
    print("Next:")
    print("  python -m script_report embed --resume   # rebuild nearest table with ATC tiebreaker")


if __name__ == "__main__":
    main()
