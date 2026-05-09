"""
download_nice_missing.py
━━━━━━━━━━━━━━━━━━━━━━━
Downloads missing NICE Technology Appraisal guidance documents from nice.org.uk
and saves metadata (TA number, drug/title, date, recommendation) to a CSV.

Run this from the NICEPDF folder, or it will auto-detect the folder.

WHAT YOU GET:
  ta{N}.pdf           — downloaded to the NICE_DIR folder
  nice_metadata.csv   — TA number, title, drug name, date, recommendation
                        (saved to PSD_database folder for the matcher)

STRATEGY:
  1. Scan NICE_DIR to find the highest TA already downloaded.
  2. Fetch https://www.nice.org.uk/guidance?type=ta&from={start} to enumerate TAs.
  3. For each TA page, extract title, date, recommendation text, and PDF URL.
  4. Download PDFs; update metadata CSV.

RATE LIMIT: 2 second delay between requests — polite scraping.

REQUIREMENTS:
  pip install requests beautifulsoup4

USAGE:
  python download_nice_missing.py
  python download_nice_missing.py --start 630   # override start TA number
  python download_nice_missing.py --metadata-only  # refresh metadata, no downloads

Re-run at any time — skips already-downloaded files.
"""

from __future__ import annotations

import csv
import sys
import re
import time
import logging
import argparse
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlencode, parse_qs

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Run:  pip install requests beautifulsoup4")
    sys.exit(1)

# ── config ────────────────────────────────────────────────────────────────────
# The NICE_DIR is the folder containing ta*.pdf files.
# Assumes this script lives in PSD_database, with NICEPDF one level up and over.
_HERE = Path(__file__).parent
NICE_DIR  = _HERE.parent.parent / "NICEPDF"
if not NICE_DIR.exists():
    # Fallback: look for a sibling NICEPDF folder
    for candidate in [_HERE.parent / "NICEPDF", _HERE / "NICEPDF", Path.cwd()]:
        if candidate.exists() and any(candidate.glob("ta*.pdf")):
            NICE_DIR = candidate
            break

META_PATH = _HERE / "nice_metadata.csv"   # saved to PSD_database for the matcher

NICE_BASE = "https://www.nice.org.uk"
DELAY     = 2.0   # seconds between requests

# How high to go — NICE TAs are currently ~TA955, check this occasionally
MAX_TA = 970

HEADERS = {
    "User-Agent": "PBAC-OpenData/1.0 (research archive; contact via github)",
    "Accept":     "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
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
_seen: set[str] = set()


def polite_get(url: str, stream: bool = False) -> "requests.Response | None":
    if url in _seen:
        return None
    _seen.add(url)
    time.sleep(DELAY)
    try:
        r = session.get(url, timeout=30, stream=stream)
        r.raise_for_status()
        return r
    except requests.RequestException as e:
        log.warning(f"  GET failed: {url} → {e}")
        return None


# ── find existing TAs ─────────────────────────────────────────────────────────
def find_existing_tas(nice_dir: Path) -> set[int]:
    """Return set of TA numbers already present in nice_dir."""
    existing = set()
    for f in nice_dir.glob("ta*.pdf"):
        m = re.match(r"ta(\d+)\.pdf$", f.name, re.IGNORECASE)
        if m:
            existing.add(int(m.group(1)))
    return existing


def find_start_ta(existing: set[int]) -> int:
    """Return the first TA number we should try to download."""
    if not existing:
        return 1
    return max(existing) + 1


# ── enumerate TAs from NICE guidance listing ──────────────────────────────────
def get_ta_numbers_from_listing(start: int, end: int) -> list[int]:
    """
    Use NICE's guidance listing to discover valid TA numbers in [start, end].
    NICE paginates 10 results per page.

    Falls back to assuming every integer in range is a valid TA (some have
    been withdrawn/replaced, which polite_get handles gracefully).
    """
    log.info(f"Enumerating NICE TAs {start}–{end} from guidance listing...")

    # NICE guidance listing: ?type=ta lists in reverse chronological order.
    # We'll assume sequential numbering and just try each one directly —
    # the guidance pages 404 cleanly for invalid/withdrawn TAs.
    return list(range(start, end + 1))


# ── scrape an individual TA guidance page ────────────────────────────────────
def scrape_ta_page(ta_num: int) -> "dict | None":
    """
    Visit https://www.nice.org.uk/guidance/ta{N} and extract:
    - title (full title including drug name)
    - drug_name (extracted from title)
    - published_date
    - recommendation_type (recommended / not recommended / optimised)
    - pdf_url

    Returns None if the page doesn't exist (404) or can't be parsed.
    """
    url = f"{NICE_BASE}/guidance/ta{ta_num}"
    resp = polite_get(url)
    if not resp:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # ── Title ──────────────────────────────────────────────────────────────
    title = ""
    for sel in ["h1", ".page-header h1", "title"]:
        el = soup.select_one(sel)
        if el:
            title = el.get_text(strip=True)
            # Strip site suffix if from <title> tag
            title = re.sub(r"\s*[|\-–]\s*NICE.*$", "", title).strip()
            if title:
                break

    if not title:
        log.warning(f"  TA{ta_num}: could not find title — skipping")
        return None

    # ── Drug name extraction ───────────────────────────────────────────────
    # Titles follow patterns like:
    #   "Pembrolizumab for treating PD-L1-positive non-small-cell lung cancer [TA447]"
    #   "Nivolumab for treating squamous non-small-cell lung cancer [TA483]"
    drug_name = ""
    m = re.match(r"^([A-Za-z][A-Za-z0-9\-]+(?:\s+[A-Za-z][A-Za-z0-9\-]+)?)\s+for\s+", title)
    if m:
        drug_name = m.group(1).lower()
    else:
        # Fallback: first word(s) before " and " or comma
        first = re.split(r"\s+(?:and|,|for)\s+", title.lower())[0]
        drug_name = first.strip()

    # ── Published date ─────────────────────────────────────────────────────
    pub_date = ""
    for pattern in [
        r"Published[:\s]+(\d{1,2}\s+\w+\s+\d{4})",
        r"(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})",
    ]:
        m2 = re.search(pattern, soup.get_text(), re.IGNORECASE)
        if m2:
            pub_date = m2.group(1)
            break

    if not pub_date:
        # Try structured metadata
        for el in soup.find_all(["time", "span", "p"], class_=re.compile(r"date|publish", re.I)):
            t = el.get_text(strip=True)
            if re.search(r"\d{4}", t):
                pub_date = t[:50]
                break

    # ── Recommendation type ────────────────────────────────────────────────
    recommendation = ""
    body_text = soup.get_text().lower()

    # Order matters — check most specific first
    if "recommended within its marketing authorisation" in body_text:
        recommendation = "Recommended"
    elif "not recommended" in body_text:
        recommendation = "Not recommended"
    elif "recommended only if" in body_text or "recommended for use" in body_text:
        recommendation = "Recommended (restricted)"
    elif "optimised" in body_text:
        recommendation = "Optimised"
    elif "recommended" in body_text:
        recommendation = "Recommended"
    elif "do not use" in body_text:
        recommendation = "Not recommended"

    # ── PDF URL ────────────────────────────────────────────────────────────
    pdf_url = None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full = urljoin(url, href)
        text = a.get_text(strip=True).lower()
        low  = full.lower()
        # Prefer links explicitly labelled as the guidance PDF
        if (
            ("pdf" in text or "download" in text or "guidance" in text)
            and (low.endswith(".pdf") or "/pdf" in low or "resources" in low)
        ):
            if low.endswith(".pdf"):
                pdf_url = full
                break
            elif not pdf_url:
                pdf_url = full   # keep looking for a direct .pdf

    # Fallback: any .pdf link on the page
    if not pdf_url:
        for a in soup.find_all("a", href=True):
            full = urljoin(url, a["href"])
            if full.lower().endswith(".pdf"):
                pdf_url = full
                break

    # Fallback: try known NICE PDF URL patterns
    if not pdf_url:
        candidates = [
            f"{NICE_BASE}/guidance/ta{ta_num}/resources/ta{ta_num}.pdf",
            f"https://nice.org.uk/guidance/ta{ta_num}/resources/ta{ta_num}.pdf",
        ]
        for c in candidates:
            # Don't actually request — just record for download attempt
            pdf_url = c
            break

    return {
        "ta_number":      ta_num,
        "title":          title,
        "drug_name":      drug_name,
        "published_date": pub_date,
        "recommendation": recommendation,
        "guidance_url":   url,
        "pdf_url":        pdf_url or "",
    }


# ── download a PDF ─────────────────────────────────────────────────────────────
def download_ta_pdf(ta_num: int, pdf_url: str, dest_dir: Path) -> bool:
    if not pdf_url:
        return False

    dest = dest_dir / f"ta{ta_num}.pdf"
    if dest.exists():
        log.info(f"    SKIP  ta{ta_num}.pdf  (already exists)")
        return True

    log.info(f"    ↓     ta{ta_num}.pdf  from {pdf_url}")
    resp = polite_get(pdf_url, stream=True)
    if not resp:
        return False

    # Check we actually got a PDF
    ct = resp.headers.get("Content-Type", "")
    if "html" in ct and "pdf" not in ct:
        log.warning(f"    ✗     ta{ta_num}: response was HTML, not PDF — PDF may need JS")
        return False

    try:
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=16_384):
                f.write(chunk)
        size_kb = dest.stat().st_size // 1024
        if size_kb < 5:
            log.warning(f"    ✗     ta{ta_num}: file too small ({size_kb} KB) — likely an error page")
            dest.unlink()
            return False
        log.info(f"    ✓     ta{ta_num}.pdf  ({size_kb} KB)")
        return True
    except OSError as e:
        log.error(f"    write error: {e}")
        dest.unlink(missing_ok=True)
        return False


# ── metadata CSV ──────────────────────────────────────────────────────────────
FIELDS = ["ta_number","title","drug_name","published_date","recommendation","guidance_url","pdf_url"]

def load_existing_metadata() -> dict[int, dict]:
    """Load existing nice_metadata.csv, keyed by ta_number."""
    if not META_PATH.exists():
        return {}
    rows = {}
    with open(META_PATH, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            try:
                rows[int(row["ta_number"])] = row
            except (KeyError, ValueError):
                pass
    return rows


def save_metadata(records: dict[int, dict]):
    """Write all records to nice_metadata.csv."""
    with open(META_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for ta_num in sorted(records.keys()):
            w.writerow(records[ta_num])
    log.info(f"Saved metadata: {META_PATH}  ({len(records)} TAs)")


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Download missing NICE Technology Appraisals")
    parser.add_argument("--start", type=int, default=None, help="First TA number to download (default: auto-detect)")
    parser.add_argument("--end",   type=int, default=MAX_TA, help=f"Last TA number to try (default: {MAX_TA})")
    parser.add_argument("--metadata-only", action="store_true", help="Refresh metadata without downloading PDFs")
    args = parser.parse_args()

    log.info("=" * 65)
    log.info("NICE Technology Appraisal Downloader")
    log.info(f"NICE folder : {NICE_DIR}")
    log.info(f"Metadata    : {META_PATH}")
    log.info("=" * 65)

    if not NICE_DIR.exists():
        log.error(f"NICE folder not found: {NICE_DIR}")
        log.error("Set NICE_DIR at the top of this script to your NICEPDF folder path.")
        sys.exit(1)

    # Find what we already have
    existing = find_existing_tas(NICE_DIR)
    log.info(f"Existing TAs in folder: {len(existing)}  (highest: {max(existing) if existing else 'none'})")

    start = args.start if args.start is not None else find_start_ta(existing)
    end   = args.end

    log.info(f"Will process TA{start} through TA{end}")

    if start > end:
        log.info("Nothing to do — already up to date.")
        return

    # Load existing metadata so we don't re-scrape pages we already have
    metadata = load_existing_metadata()
    log.info(f"Existing metadata entries: {len(metadata)}")

    ta_nums = list(range(start, end + 1))
    log.info(f"TAs to check: {len(ta_nums)}")
    log.info("")

    downloaded = 0
    skipped    = 0
    failed     = 0
    not_found  = 0

    for i, ta_num in enumerate(ta_nums, 1):
        log.info(f"[{i}/{len(ta_nums)}]  TA{ta_num}")

        # Scrape the guidance page if we don't have metadata yet
        if ta_num not in metadata:
            record = scrape_ta_page(ta_num)
            if record is None:
                log.info(f"  TA{ta_num}: not found / withdrawn — skipping")
                not_found += 1
                continue
            metadata[ta_num] = record
        else:
            record = metadata[ta_num]
            log.info(f"  TA{ta_num}: metadata cached — {record.get('drug_name','?')}")

        log.info(f"  Title  : {record['title'][:80]}")
        log.info(f"  Drug   : {record['drug_name']}")
        log.info(f"  Date   : {record['published_date']}")
        log.info(f"  Rec    : {record['recommendation'] or '(unknown)'}")

        if not args.metadata_only:
            if ta_num in existing:
                log.info(f"  PDF    : already downloaded")
                skipped += 1
            elif record.get("pdf_url"):
                ok = download_ta_pdf(ta_num, record["pdf_url"], NICE_DIR)
                if ok:
                    downloaded += 1
                    existing.add(ta_num)
                else:
                    failed += 1
            else:
                log.warning(f"  PDF    : no URL found for TA{ta_num}")
                failed += 1

        # Save metadata checkpoint every 25 TAs
        if i % 25 == 0:
            save_metadata(metadata)
            log.info(f"  (checkpoint saved)")

    # Final metadata save
    save_metadata(metadata)

    log.info("")
    log.info("=" * 65)
    log.info("FINISHED")
    log.info(f"  TAs processed : {len(ta_nums)}")
    log.info(f"  Downloaded    : {downloaded}")
    log.info(f"  Skipped       : {skipped}  (already existed)")
    log.info(f"  Failed        : {failed}")
    log.info(f"  Not found     : {not_found}  (404 / withdrawn)")
    log.info(f"  Metadata      : {META_PATH}  ({len(metadata)} total TAs)")
    log.info("=" * 65)

    if downloaded > 0:
        log.info("")
        log.info("Next: run match_pbac_nice.py to generate the cross-country comparison!")


if __name__ == "__main__":
    main()
