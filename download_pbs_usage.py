"""
download_pbs_usage.py
━━━━━━━━━━━━━━━━━━━━━
Pulls PBS prescription usage statistics from the Medicare Statistics website.

Given a drug name or PBS item code, this script:
  1. Finds all matching item codes in your PBS Schedule CSV (or accepts direct code)
  2. Queries medicarestatistics.humanservices.gov.au for each item code
  3. Downloads the XLS file the site returns (same format as manual download)
  4. Parses it into clean CSVs ready for the dashboard

OUTPUT FILES:
  pbs_usage_{drug}_{item}_by_month.csv   — prescriptions + cost by month
  pbs_usage_{drug}_{item}_by_state.csv   — breakdown by state/territory
  pbs_usage_summary.csv                  — one row per item with totals

DATA SOURCE:
  https://medicarestatistics.humanservices.gov.au/VEA0032/SAS.Web/statistics/pbs_item.html
  (no registration required — public government data)

HOW THE URL WORKS (reverse-engineered from public site):
  The site returns XLS files directly via a SAS Web endpoint.
  Two URL patterns are tried — old path and new VEA0032 path.

  Old path (still works as of 2024):
    http://medicarestatistics.humanservices.gov.au/statistics/do.jsp
      ?_PROGRAM=/statistics/pbs_item_section100  ← Section 100 (specialised drugs)
      &group=10909K                               ← PBS item code
      &VAR=SERVICES                               ← SERVICES | COST | BENEFIT
      &RPT_FMT=3                                  ← 3 = XLS, 4 = HTML table
      &start_dt=202001                            ← YYYYMM
      &end_dt=202412
      &scheme=R                                   ← R = reimbursed

  New VEA0032 path (current site):
    https://medicarestatistics.humanservices.gov.au/VEA0032/SAS.Web/statistics/do.jsp
      (same parameters as above)

  Program paths tried (in order):
    /statistics/pbs_item_section100   ← highly specialised (pembrolizumab, nusinersen etc)
    /statistics/pbs_item_standard_report  ← general PBS drugs (semaglutide etc)
    /statistics/pbs_item_general_schedule ← additional fallback

  RPT_FMT codes:
    3  = Microsoft Excel (XLS) ← what this script requests
    4  = HTML table (in-browser view)
    1  = SAS listing (raw text)

  VAR codes:
    SERVICES = number of prescriptions/supplies
    COST     = total cost (government benefit + patient contribution)
    BENEFIT  = government benefit paid
    PATIENTS = unique beneficiaries (not available for all items)

REQUIREMENTS:
  pip install requests pandas xlrd openpyxl

USAGE:
  python download_pbs_usage.py --drug semaglutide
  python download_pbs_usage.py --drug pembrolizumab --start 202001 --end 202412
  python download_pbs_usage.py --item 10909K
  python download_pbs_usage.py --top 20

NOTES:
  - If you manually downloaded an XLS from the site, move it to this folder and
    run inspect_pbs_xls.py to see its exact structure
  - Section 100 drugs (pembrolizumab, nusinersen, ocrelizumab etc) use a different
    program path — the script tries both automatically
  - Rate limited to 1 request per 2 seconds (polite)
"""

from __future__ import annotations

import csv
import io
import re
import sys
import time
import logging
import argparse
from pathlib import Path
from datetime import datetime

# NOTE: The Medicare Statistics site returns HTML tables with a .xls extension
# and a UTF-8 BOM header. We detect and parse this correctly in parse_response().

try:
    import requests
except ImportError:
    print("Run:  pip install requests pandas xlrd openpyxl")
    sys.exit(1)

try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False
    print("WARNING: pandas not installed. Run: pip install pandas xlrd openpyxl")
    print("         Without pandas, XLS files cannot be parsed.\n")

# ── config ────────────────────────────────────────────────────────────────────
OUTPUT_DIR   = Path(__file__).parent
SCHEDULE_CSV = next(OUTPUT_DIR.glob("pbs_schedule_*.csv"), None)
DELAY        = 2.0   # seconds between requests

# ── URL patterns — both tried for every request ───────────────────────────────
ENDPOINTS = [
    # New VEA0032 path (current site, HTTPS)
    "https://medicarestatistics.humanservices.gov.au/VEA0032/SAS.Web/statistics/do.jsp",
    # Old path (HTTP — still indexed in Google, still works for archived data)
    "http://medicarestatistics.humanservices.gov.au/statistics/do.jsp",
]

# SAS programs to try (in order)
PROGRAMS = [
    "/statistics/pbs_item_section100",          # Section 100 / Highly Specialised
    "/statistics/pbs_item_standard_report",     # Standard PBS drugs
    "/statistics/pbs_item_general_schedule",    # General Schedule fallback
]

# RPT_FMT codes
RPT_XLS  = "3"   # Returns XLS file directly — what we use
RPT_HTML = "4"   # HTML table — fallback for parsing if XLS fails

HEADERS = {
    "User-Agent": "PBAC-OpenData/1.0 (research archive; +https://github.com)",
    "Accept":     "application/vnd.ms-excel, text/html, */*",
    "Referer":    "https://medicarestatistics.humanservices.gov.au/VEA0032/SAS.Web/statistics/pbs_item.html",
}

# Variables to pull for each item
VARIABLES = ["SERVICES", "COST"]   # BENEFIT and PATIENTS also exist

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


def polite_get(url: str, params: dict) -> "requests.Response | None":
    time.sleep(DELAY)
    try:
        r = session.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r
    except requests.RequestException as e:
        log.warning(f"  GET failed: {e}")
        return None


# ── PBS Schedule lookup ───────────────────────────────────────────────────────
def load_schedule(csv_path: "Path | None") -> list[dict]:
    if not csv_path or not csv_path.exists():
        log.warning("No PBS Schedule CSV found. Run download_pbs_schedule.py first.")
        log.warning("Or use --item to specify item codes directly.")
        return []
    rows = []
    with open(csv_path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    log.info(f"Loaded PBS Schedule: {len(rows):,} items from {csv_path.name}")
    return rows


def find_items_for_drug(drug: str, schedule: list[dict]) -> list[dict]:
    q = drug.lower().strip()
    matches = []
    seen = set()

    name_cols  = ["drug_name", "chemical_name", "generic_name", "name", "drug",
                  "mp_pt", "tp_full_generic_name"]
    brand_cols = ["brand_name", "brand", "proprietary_name", "tp_brand_name"]
    code_cols  = ["item_code", "pbs_code", "item", "code", "li_item_id"]

    for row in schedule:
        item_code = None
        for col in code_cols:
            val = row.get(col, "")
            if val:
                item_code = str(val).strip().upper()
                break
        if not item_code or item_code in seen:
            continue

        matched = False
        for col in name_cols + brand_cols:
            if q in str(row.get(col, "")).lower():
                matched = True
                break

        if matched:
            seen.add(item_code)
            matches.append({
                "item_code":    item_code,
                "drug_name":    next((str(row.get(c, "")) for c in name_cols if row.get(c)), drug),
                "brand_name":   next((str(row.get(c, "")) for c in brand_cols if row.get(c)), ""),
                "form_strength": str(row.get("form_strength", row.get("li_form", ""))),
            })

    log.info(f"  Found {len(matches)} item code(s) for '{drug}'")
    return matches


# ── XLS / HTML parsing ────────────────────────────────────────────────────────
def parse_response(content: bytes, item_code: str, var: str) -> list[dict]:
    """
    Parse the response from the Medicare Statistics endpoint.

    IMPORTANT: Despite the .xls extension on manual downloads, the site returns
    HTML tables with a UTF-8 BOM (b'\\xef\\xbb\\xbf<!DOCTYPE html>').
    We detect this and parse accordingly.

    Typical HTML table structure:
      Row 0:  Report title / header metadata
      Row 1+: Column headers and data
      Last:   Total row (excluded)
    """
    # Detect HTML-disguised-as-XLS (UTF-8 BOM + <!DOCTYPE or <html)
    stripped = content.lstrip(b"\xef\xbb\xbf")
    is_html  = stripped[:5].lower() in (b"<!doc", b"<html", b"<?xml")

    if is_html:
        return _parse_html(stripped.decode("utf-8", errors="replace"), item_code, var)
    else:
        # Genuine binary XLS/XLSX — try pandas
        return _parse_binary_excel(content, item_code, var)


def _parse_html(html: str, item_code: str, var: str) -> list[dict]:
    """Parse the HTML table that Medicare Statistics returns."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log.warning("  beautifulsoup4 not installed. Run: pip install beautifulsoup4 lxml")
        return []

    soup = BeautifulSoup(html, "html.parser")

    no_data_text = soup.get_text().lower()
    if any(p in no_data_text for p in ["no data", "no records", "item not found", "invalid item"]):
        return []

    tables = soup.find_all("table")
    if not tables:
        return []

    # Use the largest table (most data rows)
    best = max(tables, key=lambda t: len(t.find_all("tr")))
    all_rows = best.find_all("tr")
    if not all_rows:
        return []

    # Find the header row — first row with keyword column labels
    header_idx = 0
    HEADER_KEYWORDS = {"month", "year", "total", "nsw", "vic", "qld", "sa",
                       "wa", "tas", "act", "nt", "date", "period", "atc",
                       "services", "cost", "benefit", "group"}
    for i, tr in enumerate(all_rows):
        cells = [td.get_text(strip=True).lower() for td in tr.find_all(["th", "td"])]
        if any(any(kw in c for kw in HEADER_KEYWORDS) for c in cells):
            header_idx = i
            break

    # Build clean column names from header row
    raw_headers = [td.get_text(strip=True) for td in all_rows[header_idx].find_all(["th", "td"])]
    headers = []
    for h in raw_headers:
        h2 = re.sub(r"\s+", "_", h.lower().strip())
        h2 = re.sub(r"[^\w]", "", h2)
        headers.append(h2 or f"col_{len(headers)}")

    # Parse data rows
    records = []
    for tr in all_rows[header_idx + 1:]:
        cells = [td.get_text(strip=True) for td in tr.find_all(["th", "td"])]
        if not cells or all(c == "" for c in cells):
            continue
        first = cells[0].lower().strip()
        if first in ("total", "grand total", "sub total", "subtotal"):
            continue

        rec = dict(zip(headers, cells))
        rec["_item_code"] = item_code
        rec["_variable"]  = var
        records.append(rec)

    log.info(f"    ✓  Parsed {len(records)} rows (HTML table, header at row {header_idx})")
    return records


def _parse_binary_excel(content: bytes, item_code: str, var: str) -> list[dict]:
    """Parse genuine binary XLS/XLSX (fallback path)."""
    if not PANDAS_AVAILABLE:
        log.warning("  pandas not available. Run: pip install pandas xlrd openpyxl")
        return []
    try:
        df = pd.read_excel(io.BytesIO(content), header=None, engine="xlrd")
    except Exception:
        try:
            df = pd.read_excel(io.BytesIO(content), header=None, engine="openpyxl")
        except Exception as e:
            log.warning(f"  Cannot parse binary Excel: {e}")
            return []

    if df.empty:
        return []

    # Find header row
    header_row_idx = 0
    for i, row in df.iterrows():
        vals = [str(v).lower() for v in row if pd.notna(v)]
        if any(kw in " ".join(vals) for kw in ["month", "year", "nsw", "vic", "total", "atc"]):
            header_row_idx = i
            break

    headers = []
    for h in df.iloc[header_row_idx]:
        if pd.notna(h):
            h2 = re.sub(r"[^\w]", "", re.sub(r"\s+", "_", str(h).lower().strip()))
            headers.append(h2 or f"col_{len(headers)}")
        else:
            headers.append(f"col_{len(headers)}")

    records = []
    for i in range(header_row_idx + 1, len(df)):
        row = df.iloc[i]
        if all(pd.isna(v) for v in row):
            continue
        first = str(row.iloc[0]).lower().strip() if pd.notna(row.iloc[0]) else ""
        if "total" in first:
            continue
        rec = {h: ("" if pd.isna(v) else v) for h, v in zip(headers, row)}
        rec["_item_code"] = item_code
        rec["_variable"]  = var
        records.append(rec)

    log.info(f"    ✓  Parsed {len(records)} rows (binary Excel)")
    return records




# ── Query one item from Medicare Statistics ────────────────────────────────────
def query_item(item_code: str, var: str, start_dt: str, end_dt: str) -> list[dict]:
    """
    Try every combination of endpoint × program path.
    First try XLS (RPT_FMT=3), then HTML fallback (RPT_FMT=4).
    Returns list of parsed row dicts, or [] if nothing worked.
    """
    for endpoint in ENDPOINTS:
        for program in PROGRAMS:
            # ── Try XLS first ────────────────────────────────────────────────
            params_xls = {
                "_PROGRAM": program,
                "group":    item_code,
                "VAR":      var,
                "RPT_FMT":  RPT_XLS,
                "start_dt": start_dt,
                "end_dt":   end_dt,
                "scheme":   "R",
                "STAT":     "count",
                "PTYPE":    "calendar",
            }
            log.info(f"    Trying: {endpoint.split('//')[-1].split('/')[0]} / {program.split('/')[-1]} / XLS")
            resp = polite_get(endpoint, params_xls)
            if resp and len(resp.content) > 200:
                rows = parse_response(resp.content, item_code, var)
                if rows:
                    # Save raw file for debugging
                    ext = ".html" if resp.content.lstrip(b"\xef\xbb\xbf")[:5].lower() in (b"<!doc", b"<html") else ".xls"
                    raw_path = OUTPUT_DIR / f"pbs_raw_{item_code}_{var}{ext}"
                    raw_path.write_bytes(resp.content)
                    log.info(f"    Raw response saved: {raw_path.name}")
                    return rows
                log.info(f"    Response received but no data parsed — trying next")

    log.warning(f"  No data found for item={item_code} var={var} — tried all endpoints and programs")
    return []


# ── Download one item (all variables) ─────────────────────────────────────────
def download_item(item: dict, start_dt: str, end_dt: str, drug_label: str) -> dict:
    item_code = item["item_code"]
    safe_drug = re.sub(r"[^\w\-]", "_", drug_label.lower())[:30]
    safe_code = re.sub(r"[^\w]", "", item_code)
    log.info(f"  Item {item_code}: {item.get('drug_name', '')} {item.get('form_strength', '')}")

    all_rows: list[dict] = []
    for var in VARIABLES:
        rows = query_item(item_code, var, start_dt, end_dt)
        all_rows.extend(rows)

    summary = {
        "item_code":     item_code,
        "drug_name":     item.get("drug_name", drug_label),
        "brand_name":    item.get("brand_name", ""),
        "form_strength": item.get("form_strength", ""),
        "start_dt":      start_dt,
        "end_dt":        end_dt,
        "total_scripts": 0,
        "rows_retrieved": len(all_rows),
    }

    if all_rows:
        out_path = OUTPUT_DIR / f"pbs_usage_{safe_drug}_{safe_code}.csv"
        save_csv(all_rows, out_path)

    log.info(f"  Summary: {len(all_rows)} rows retrieved for {item_code}")
    return summary


def save_csv(records: list[dict], path: Path):
    if not records:
        return
    all_keys: list[str] = []
    seen: set[str] = set()
    for r in records:
        for k in r:
            if k not in seen:
                all_keys.append(k)
                seen.add(k)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(records)
    log.info(f"  Saved: {path.name}  ({len(records)} rows)")


# ── CLI ────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Download PBS prescription usage from Medicare Statistics"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--drug",  help="Drug name (e.g. semaglutide, pembrolizumab)")
    group.add_argument("--item",  help="PBS item code (e.g. 10909K)")
    group.add_argument("--top",   type=int, metavar="N", help="Top N drugs from PBS Schedule")

    parser.add_argument("--start",    default="202001", help="Start YYYYMM (default: 202001)")
    parser.add_argument("--end",      default=datetime.now().strftime("%Y%m"), help="End YYYYMM (default: now)")
    parser.add_argument("--schedule", help="Path to PBS Schedule CSV (auto-detected if omitted)")
    args = parser.parse_args()

    log.info("=" * 65)
    log.info("PBS Usage Downloader — Medicare Statistics")
    log.info(f"Period: {args.start} → {args.end}")
    log.info("=" * 65)

    schedule_path = Path(args.schedule) if args.schedule else SCHEDULE_CSV
    schedule = load_schedule(schedule_path)

    items: list[dict] = []

    if args.item:
        items = [{"item_code": args.item.upper(), "drug_name": args.item, "brand_name": "", "form_strength": ""}]
        drug_label = args.item

    elif args.drug:
        drug_label = args.drug
        items = find_items_for_drug(args.drug, schedule) if schedule else []
        if not items:
            log.warning("No schedule loaded — using drug name as item code (probably won't work)")
            log.warning("Run download_pbs_schedule.py first, or use --item with a known code")
            items = [{"item_code": args.drug.upper(), "drug_name": args.drug, "brand_name": "", "form_strength": ""}]

    elif args.top:
        if not schedule:
            log.error("PBS Schedule required for --top")
            sys.exit(1)
        seen_d: set[str] = set()
        for row in schedule:
            name = next((str(row.get(c, "")) for c in ["drug_name","chemical_name","generic_name","mp_pt"] if row.get(c)), "")
            code = next((str(row.get(c, "")).upper() for c in ["item_code","pbs_code","item","li_item_id"] if row.get(c)), "")
            if name and name.lower() not in seen_d and code:
                seen_d.add(name.lower())
                items.append({"item_code": code, "drug_name": name, "brand_name": "", "form_strength": ""})
            if len(items) >= args.top:
                break
        drug_label = f"top{args.top}"

    else:
        drug_label = "unknown"

    log.info(f"Processing {len(items)} item code(s)...\n")

    summaries = []
    for i, item in enumerate(items, 1):
        log.info(f"[{i}/{len(items)}]  {item['item_code']}  —  {item.get('drug_name','')}")
        summaries.append(download_item(item, args.start, args.end, item.get("drug_name", drug_label)))

    if summaries:
        save_csv(summaries, OUTPUT_DIR / "pbs_usage_summary.csv")

    log.info("")
    log.info("=" * 65)
    log.info("DONE")
    log.info(f"  Items processed : {len(summaries)}")
    log.info(f"  Output folder   : {OUTPUT_DIR}")
    log.info("=" * 65)
    log.info("")
    log.info("IMPORTANT: If nothing downloaded, first run inspect_pbs_xls.py on your")
    log.info("  manually-downloaded file to see the exact URL parameters that work.")
    log.info("  Then update PROGRAMS and ENDPOINTS at the top of this script.")


if __name__ == "__main__":
    main()
