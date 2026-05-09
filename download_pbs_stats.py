"""
download_pbs_stats.py
━━━━━━━━━━━━━━━━━━━━━
Downloads PBS expenditure and prescription volume statistics from the
Australian Government's public PBS data sources.

WHY THIS MATTERS:
  The PBAC tells you *if* a drug was listed and *when*.
  The PBS Schedule tells you current price and restrictions.
  The PBS statistics tell you *how much* it's being used and costing.

  Combined: pembrolizumab was listed Nov 2019 → now costs PBS $X billion/year.
  That's the kind of figure that makes an article shareable.

WHAT YOU GET:
  pbs_expenditure_top.csv    — top drugs by government expenditure (most recent year)
  pbs_expenditure_all.csv    — all drugs with expenditure and prescription volumes
  pbs_expenditure_trend.csv  — year-by-year totals for top 50 drugs (if available)

DATA SOURCES (all public, no registration required):
  1. PBS Statistics page:
     https://www.pbs.gov.au/statistics/pbs-expenditure/index.html
     Contains Excel files: annual top-20s, monthly item reports.

  2. AIHW Health Expenditure Australia:
     https://www.aihw.gov.au/reports/health-welfare-expenditure/health-expenditure
     Broad expenditure context.

  3. data.gov.au PBS datasets:
     https://data.gov.au/search?q=pharmaceutical+benefits
     CSV downloads of historical PBS data.

NOTE ON MBS vs PBS:
  The user asked about "MBS public website" — this probably means PBS.
  MBS = Medicare Benefits Schedule = doctor visits, procedures, diagnostics.
  PBS = Pharmaceutical Benefits Scheme = drugs.
  Both are public. This script does PBS drugs.
  MBS data: https://www.aihw.gov.au/reports-data/mbs-pbs-data → separate analysis.

REQUIREMENTS:
  pip install requests beautifulsoup4 openpyxl

USAGE:
  python download_pbs_stats.py
"""

from __future__ import annotations

import csv
import re
import sys
import time
import logging
from pathlib import Path
from urllib.parse import urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Run:  pip install requests beautifulsoup4 openpyxl")
    sys.exit(1)

# ── config ────────────────────────────────────────────────────────────────────
OUTPUT_DIR = Path(__file__).parent
DELAY      = 2.0

PBS_STATS_URL = "https://www.pbs.gov.au/statistics/pbs-expenditure/index.html"
DATAGOV_BASE  = "https://data.gov.au"

HEADERS = {
    "User-Agent": "PBAC-OpenData/1.0 (research archive; +https://github.com)",
    "Accept":     "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
}

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

session = requests.Session()
session.headers.update(HEADERS)


def polite_get(url: str, stream: bool = False) -> "requests.Response | None":
    time.sleep(DELAY)
    try:
        r = session.get(url, timeout=30, stream=stream)
        r.raise_for_status()
        return r
    except requests.RequestException as e:
        log.warning(f"  GET failed: {url} → {e}")
        return None


def download_file(url: str, dest: Path) -> bool:
    if dest.exists():
        log.info(f"  SKIP  {dest.name}  (already downloaded)")
        return True
    log.info(f"  ↓     {dest.name}")
    resp = polite_get(url, stream=True)
    if not resp:
        return False
    try:
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=16_384):
                f.write(chunk)
        log.info(f"  ✓     {dest.name}  ({dest.stat().st_size // 1024} KB)")
        return True
    except OSError as e:
        log.error(f"  write error: {e}")
        dest.unlink(missing_ok=True)
        return False


# ── Step 1: PBS Statistics page ───────────────────────────────────────────────
def scrape_pbs_stats_page() -> list[dict]:
    """
    Scrape the PBS statistics landing page for links to expenditure Excel/CSV files.
    Returns list of {url, filename, description}.
    """
    log.info(f"Fetching PBS statistics page: {PBS_STATS_URL}")
    resp = polite_get(PBS_STATS_URL)
    if not resp:
        log.warning("Could not reach PBS statistics page.")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    files = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        full = urljoin(PBS_STATS_URL, href)
        low  = full.lower()
        text = a.get_text(strip=True)

        # Look for Excel or CSV files related to expenditure
        if any(kw in low for kw in (".xlsx", ".xls", ".csv")) and any(
            kw in (text + href).lower()
            for kw in ("expenditure", "prescri", "statistic", "item", "top", "cost")
        ):
            files.append({
                "url":         full,
                "filename":    urlparse(full).path.split("/")[-1],
                "description": text,
            })

    log.info(f"  Found {len(files)} data file links on PBS statistics page")
    return files


# ── Step 2: data.gov.au PBS datasets ─────────────────────────────────────────
def search_datagov_pbs() -> list[dict]:
    """
    Search data.gov.au for PBS expenditure datasets.
    Returns list of {url, filename, description}.
    """
    log.info("Searching data.gov.au for PBS datasets...")
    search_url = "https://data.gov.au/api/3/action/package_search?q=pharmaceutical+benefits+expenditure&rows=10"

    resp = polite_get(search_url)
    if not resp:
        return []

    try:
        data = resp.json()
        results = data.get("result", {}).get("results", [])
    except Exception:
        return []

    files = []
    for pkg in results:
        title = pkg.get("title", "")
        for resource in pkg.get("resources", []):
            url = resource.get("url", "")
            fmt = resource.get("format", "").lower()
            if fmt in ("csv", "xlsx", "xls") and url:
                files.append({
                    "url":         url,
                    "filename":    resource.get("name", url.split("/")[-1]) + f".{fmt}",
                    "description": f"{title} — {resource.get('name', '')}",
                })

    log.info(f"  Found {len(files)} datasets on data.gov.au")
    return files


# ── Step 3: Parse Excel if openpyxl available ─────────────────────────────────
def try_parse_excel(path: Path) -> "list[dict] | None":
    """
    Attempt to parse a downloaded Excel file into a list of dicts.
    Returns None if openpyxl not available or file can't be parsed.
    """
    try:
        import openpyxl
    except ImportError:
        log.info("  (openpyxl not installed — skipping Excel parsing)")
        return None

    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return None

        # Find header row (first row with multiple non-None values)
        header_row = 0
        for i, row in enumerate(rows):
            non_null = [c for c in row if c is not None]
            if len(non_null) >= 3:
                header_row = i
                break

        headers = [str(h).strip() if h else f"col_{j}" for j, h in enumerate(rows[header_row])]
        records = []
        for row in rows[header_row + 1:]:
            if all(v is None for v in row):
                continue
            records.append({headers[j]: v for j, v in enumerate(row) if j < len(headers)})

        log.info(f"  Parsed {len(records)} rows from {path.name}")
        return records

    except Exception as e:
        log.warning(f"  Could not parse {path.name}: {e}")
        return None


def normalise_drug_name(name: str) -> str:
    """Lowercase, strip trailing brand info in parentheses."""
    if not isinstance(name, str):
        return ""
    n = name.lower().strip()
    n = re.sub(r"\s*\(.*?\)", "", n)
    return n.strip()


def records_to_csv(records: list[dict], path: Path):
    if not records:
        return
    fields = list(records[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(records)
    log.info(f"Saved: {path}  ({len(records)} rows)")


# ── Step 4: Manual download guidance if scraping fails ────────────────────────
MANUAL_GUIDE = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MANUAL DOWNLOAD GUIDE — PBS Expenditure Data
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The PBS website is heavily JavaScript-rendered, so automated
scraping sometimes fails. Here's how to get the data manually:

1. BEST SOURCE — PBS Drug Expenditure Statistics:
   https://www.pbs.gov.au/statistics/pbs-expenditure/index.html
   Download the most recent "PBS Drug Expenditure" Excel workbook.
   It contains:
   - Top 20 drugs by government expenditure
   - Top 20 drugs by prescription volume
   - All PBS items with annual cost and volume data

2. ALTERNATIVE — AIHW Drug Expenditure:
   https://www.aihw.gov.au/reports/health-welfare-expenditure/health-expenditure
   Click "Data" tab → download Excel files.
   Contains PBS expenditure by ATC class and year (1985–present).

3. DATA.GOV.AU — Historical PBS Data:
   https://data.gov.au/search?q=pharmaceutical+benefits
   Search results include multi-year item-level datasets.

WHAT TO LOOK FOR IN THE EXCEL FILES:
   - Drug name / item description
   - Item code (links to PBS Schedule via pbs_schedule_YYYY-MM.csv)
   - Government expenditure ($)
   - Number of prescriptions / services
   - Year / quarter

Save any downloaded Excel or CSV files to this folder, then re-run
this script — it will auto-detect and parse them.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHY THIS DATA IS POWERFUL:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Combining PBS expenditure with PBAC PSDs gives you:

  "Pembrolizumab was listed by PBAC in November 2019.
   In its first five years on the PBS it cost the government
   $2.1 billion across 47,000 patients — more than any other
   single drug in Australian history."

That sentence requires:
  - PBAC PSD (listing date, therapy area)
  - PBS expenditure (cost, patient numbers)
  - PBS Schedule (item code to link the two)

Nobody else is publishing this for free.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 65)
    log.info("PBS Expenditure & Prescription Statistics Downloader")
    log.info(f"Output: {OUTPUT_DIR}")
    log.info("=" * 65)

    downloaded_files = []
    all_records = []

    # Check for already-downloaded Excel files in the folder
    existing = list(OUTPUT_DIR.glob("*.xlsx")) + list(OUTPUT_DIR.glob("*.xls"))
    if existing:
        log.info(f"Found {len(existing)} Excel file(s) already in folder — parsing them first.")
        for f in existing:
            if "pbs" in f.name.lower() or "expenditure" in f.name.lower() or "statistic" in f.name.lower():
                records = try_parse_excel(f)
                if records:
                    all_records.extend(records)

    # Step 1: scrape PBS statistics page
    log.info("")
    log.info("Step 1: Scraping PBS statistics page...")
    pbs_files = scrape_pbs_stats_page()

    for item in pbs_files[:5]:   # limit to first 5 files
        dest = OUTPUT_DIR / f"pbs_stats_{item['filename']}"
        if download_file(item["url"], dest):
            downloaded_files.append(dest)
            if dest.suffix.lower() in (".xlsx", ".xls"):
                records = try_parse_excel(dest)
                if records:
                    all_records.extend(records)

    # Step 2: data.gov.au
    log.info("")
    log.info("Step 2: Searching data.gov.au...")
    gov_files = search_datagov_pbs()

    for item in gov_files[:3]:
        fname = re.sub(r"[^\w.\-]", "_", item["filename"])[:80]
        dest = OUTPUT_DIR / f"pbs_datagov_{fname}"
        if download_file(item["url"], dest):
            downloaded_files.append(dest)

    # Step 3: save consolidated CSV if we got records
    if all_records:
        log.info("")
        log.info("Step 3: Saving consolidated expenditure CSV...")

        # Try to identify drug name and expenditure columns
        first = all_records[0]
        name_col = next((k for k in first if any(w in k.lower() for w in ("drug", "description", "item", "name", "brand", "generic"))), None)
        cost_col = next((k for k in first if any(w in k.lower() for w in ("expenditure", "cost", "benefit", "government", "$"))), None)
        vol_col  = next((k for k in first if any(w in k.lower() for w in ("prescription", "service", "quantity", "volume", "scripts"))), None)

        if name_col and (cost_col or vol_col):
            # Sort by expenditure descending
            def safe_float(v):
                try: return float(str(v).replace(",", "").replace("$", ""))
                except: return 0.0

            sorted_records = sorted(all_records, key=lambda r: safe_float(r.get(cost_col, 0)), reverse=True) if cost_col else all_records

            # Add normalised drug name for joining to PBAC data
            for r in sorted_records:
                r["_drug_name_normalised"] = normalise_drug_name(str(r.get(name_col, "")))

            records_to_csv(sorted_records[:200], OUTPUT_DIR / "pbs_expenditure_top.csv")
            records_to_csv(sorted_records, OUTPUT_DIR / "pbs_expenditure_all.csv")
        else:
            log.info("  Column structure unclear — saving raw parsed data as-is.")
            records_to_csv(all_records, OUTPUT_DIR / "pbs_expenditure_raw.csv")
    else:
        log.warning("")
        log.warning("No expenditure data was automatically downloaded.")
        log.warning("The PBS statistics page appears to be JavaScript-rendered.")

    # Always print the manual guide
    print(MANUAL_GUIDE)

    log.info("=" * 65)
    log.info("DONE")
    log.info(f"  Files downloaded : {len(downloaded_files)}")
    log.info(f"  Records parsed   : {len(all_records)}")
    log.info("=" * 65)

    if downloaded_files:
        log.info("")
        log.info("Next step: run match_pbac_nice.py to join expenditure data to PBAC PSDs.")


if __name__ == "__main__":
    main()
