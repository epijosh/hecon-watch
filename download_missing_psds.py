"""
download_missing_psds.py
━━━━━━━━━━━━━━━━━━━━━━━━
Politely downloads PBAC Public Summary Documents from pbs.gov.au
for the 6 PBAC meetings missing from this collection (Mar 2024 – Nov 2025).

STRATEGY:
  The per-meeting listing pages no longer exist after the 2024 PBS redesign.
  Instead, this script starts from the master "public-summary-documents-by-product"
  index (which lists every PSD ever published) and the main PSD landing page,
  then filters links to only the target meeting codes.

POLITE SCRAPING RULES:
  - 1.5 second delay between every request
  - Identifies itself honestly via User-Agent
  - Skips files you already have
  - Dedup guard so no URL is fetched twice

REQUIREMENTS:
  pip install requests beautifulsoup4

USAGE:
  python download_missing_psds.py

  Saves PDFs into the same folder as this script. Writes download_log.txt.
  If downloads fail, re-run — it skips already-downloaded files.
"""

from __future__ import annotations

import sys
import time
import logging
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("\n  Missing dependencies. Run:  pip install requests beautifulsoup4\n")
    sys.exit(1)

# ── config ────────────────────────────────────────────────────────────────────
OUTPUT_DIR    = Path(__file__).parent
REQUEST_DELAY = 1.5          # seconds between requests — be polite
TIMEOUT       = 30

PBS_BASE  = "https://www.pbs.gov.au"
PBS_MEDIA = "https://m.pbs.gov.au"   # actual PDF/DOCX files live here

# Meetings to download — YYYY-MM format
MISSING_MEETINGS = [
    "2024-03",   # March 2024
    "2024-07",   # July 2024
    "2024-11",   # November 2024
    "2025-03",   # March 2025
    "2025-07",   # July 2025
    "2025-11",   # November 2025
]

# Pages to crawl looking for PSD links — ordered by most likely to work first
DISCOVERY_PAGES = [
    # Master index — lists every PSD ever published (try both URL variants)
    "/info/industry/listing/elements/pbac-meetings/psd/public-summary-documents-by-product",
    "/pbs/industry/listing/elements/pbac-meetings/psd/public-summary-documents-by-product",
    # Main PSD landing page — may list recent meetings with links
    "/info/industry/listing/elements/pbac-meetings/psd",
    "/pbs/industry/listing/elements/pbac-meetings/psd",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; PBAC-PSD-Archiver/1.0; "
        "personal research archive; +https://www.pbs.gov.au)"
    ),
    "Accept":          "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}

MONTH_LABELS = {
    "01": "January",  "02": "February", "03": "March",
    "04": "April",    "05": "May",      "06": "June",
    "07": "July",     "08": "August",   "09": "September",
    "10": "October",  "11": "November", "12": "December",
}

# ── logging ───────────────────────────────────────────────────────────────────
log_path = OUTPUT_DIR / "download_log.txt"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(log_path, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── HTTP ──────────────────────────────────────────────────────────────────────
session = requests.Session()
session.headers.update(HEADERS)
_fetched: set = set()   # dedup — never hit the same URL twice


def polite_get(url: str, stream: bool = False) -> requests.Response | None:
    if url in _fetched:
        return None
    _fetched.add(url)
    time.sleep(REQUEST_DELAY)
    try:
        r = session.get(url, timeout=TIMEOUT, stream=stream)
        r.raise_for_status()
        return r
    except requests.RequestException as e:
        log.error(f"  GET failed: {url}")
        log.error(f"           → {e}")
        return None


def polite_head(url: str) -> int:
    """Return HTTP status code from a HEAD request, or 0 on error."""
    time.sleep(REQUEST_DELAY)
    try:
        r = session.head(url, timeout=10)
        return r.status_code
    except requests.RequestException:
        return 0


# ── step 1: discover PSD page URLs for target meetings ───────────────────────
def is_psd_page(url: str, meeting_code: str) -> bool:
    """True if URL looks like an individual PSD HTML page for this meeting."""
    return (
        f"/psd/{meeting_code}/" in url
        and not any(url.lower().endswith(x) for x in (".pdf", ".docx", ".doc", ".xlsx"))
    )


def crawl_page_for_psd_links(page_url: str) -> dict[str, list[str]]:
    """
    Fetch one page and return {meeting_code: [psd_html_url, ...]} for all
    target meetings found in its links.
    """
    log.info(f"  Crawling: {page_url}")
    resp = polite_get(page_url)
    if not resp:
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    result: dict[str, list[str]] = {code: [] for code in MISSING_MEETINGS}
    found_any = False

    for a in soup.find_all("a", href=True):
        full = urljoin(PBS_BASE, a["href"])
        for code in MISSING_MEETINGS:
            if is_psd_page(full, code) and full not in result[code]:
                result[code].append(full)
                found_any = True

    total = sum(len(v) for v in result.values())
    log.info(f"  → Found {total} target PSD links across all meetings")
    return result if found_any else {}


def discover_all_psd_links() -> dict[str, list[str]]:
    """
    Try each discovery page in order. Merge all results.
    Returns {meeting_code: [psd_html_url, ...]}
    """
    merged: dict[str, list[str]] = {code: [] for code in MISSING_MEETINGS}

    for path in DISCOVERY_PAGES:
        url = PBS_BASE + path
        partial = crawl_page_for_psd_links(url)
        for code, links in partial.items():
            for link in links:
                if link not in merged[code]:
                    merged[code].append(link)

    return merged


# ── step 2: find the downloadable file on a PSD detail page ──────────────────
def get_file_url_from_page(psd_page_url: str) -> str | None:
    """Visit a PSD HTML page and return the PDF (or DOCX) download URL."""
    resp = polite_get(psd_page_url)
    if not resp:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    pdf_url = docx_url = None

    for a in soup.find_all("a", href=True):
        full = urljoin(psd_page_url, a["href"])
        low  = full.lower()
        if low.endswith(".pdf")  and not pdf_url:
            pdf_url = full
        if low.endswith(".docx") and not docx_url:
            docx_url = full

    return pdf_url or docx_url


def guess_file_url(psd_page_url: str, meeting_code: str) -> str | None:
    """
    If no file link found on the HTML page, try constructing the m.pbs.gov.au
    file URL directly from the page slug.

    Known pattern from real URLs:
      HTML: /info/…/psd/2024-11/enzalutamide-PSD-November-2024
      File: m.pbs.gov.au/…/psd/2024-11/files/enzalutamide-psd-nov-2024.pdf
    """
    slug = psd_page_url.rstrip("/").split("/")[-1]
    # Normalise: lowercase, replace month name with abbreviation
    month_abbrevs = {
        "january":"jan","february":"feb","march":"mar","april":"apr",
        "may":"may","june":"jun","july":"jul","august":"aug",
        "september":"sep","october":"oct","november":"nov","december":"dec",
    }
    slug_low = slug.lower()
    for full_m, abbr in month_abbrevs.items():
        slug_low = slug_low.replace(f"-{full_m}-", f"-{abbr}-")

    year = meeting_code.split("-")[0]
    # Try both the slug as-is and the normalised version
    candidates_stems = list(dict.fromkeys([slug_low, slug.lower()]))  # dedup, keep order

    for stem in candidates_stems:
        for ext in (".pdf", ".docx"):
            candidate = (
                f"{PBS_MEDIA}/industry/listing/elements"
                f"/pbac-meetings/psd/{meeting_code}/files/{stem}{ext}"
            )
            status = polite_head(candidate)
            if status == 200:
                log.info(f"    (direct file URL found: {candidate})")
                return candidate

    return None


# ── step 3: download ──────────────────────────────────────────────────────────
def make_dest(file_url: str) -> Path:
    fname = unquote(urlparse(file_url).path.split("/")[-1])
    return OUTPUT_DIR / fname


def download_file(url: str, dest: Path) -> bool:
    if dest.exists():
        log.info(f"    SKIP  {dest.name}  (already in collection)")
        return True

    log.info(f"    ↓     {dest.name}")
    resp = polite_get(url, stream=True)
    if not resp:
        return False

    try:
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=16_384):
                f.write(chunk)
        log.info(f"    ✓     {dest.name}  ({dest.stat().st_size // 1024} KB)")
        return True
    except OSError as e:
        log.error(f"    write error: {e}")
        dest.unlink(missing_ok=True)
        return False


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 65)
    log.info("PBAC PSD Downloader  —  polite archiving script")
    log.info(f"Output: {OUTPUT_DIR}")
    log.info("=" * 65)
    log.info("")
    log.info("Step 1: Discovering PSD links from PBS master index...")

    by_meeting = discover_all_psd_links()

    total_found = sum(len(v) for v in by_meeting.values())
    log.info(f"  Total PSD pages discovered: {total_found}")

    if total_found == 0:
        log.warning("")
        log.warning("  ⚠  No PSD links found from any discovery page.")
        log.warning("  This likely means the PBS website is now JavaScript-rendered")
        log.warning("  and requests+BeautifulSoup can't see the content.")
        log.warning("")
        log.warning("  MANUAL ALTERNATIVE:")
        log.warning("  1. Visit https://www.pbs.gov.au/info/industry/listing/elements/pbac-meetings/psd")
        log.warning("  2. Click each meeting (2024-03 through 2025-11)")
        log.warning("  3. Download each PDF to this folder")
        log.warning("")
        log.warning("  OR install Playwright and re-run with JS support:")
        log.warning("     pip install playwright && python -m playwright install chromium")
        log.warning("  (then ask Claude to update this script to use Playwright)")
        return

    log.info("")
    log.info("Step 2: Downloading PDFs...")

    downloaded = skipped = failed = 0

    for meeting_code in MISSING_MEETINGS:
        pages = by_meeting.get(meeting_code, [])
        year, mo = meeting_code.split("-")
        label = f"{MONTH_LABELS[mo]} {year}"

        log.info("")
        log.info(f"━━━  {label} PBAC Meeting  ({meeting_code})  —  {len(pages)} PSDs  ━━━")

        if not pages:
            log.warning(f"  No PSDs found for {meeting_code}")
            continue

        for page_url in pages:
            file_url = get_file_url_from_page(page_url)

            if not file_url:
                log.warning(f"  No file link on page: {page_url}")
                file_url = guess_file_url(page_url, meeting_code)

            if not file_url:
                log.error(f"  Giving up on: {page_url}")
                failed += 1
                continue

            dest = make_dest(file_url)
            if dest.exists():
                skipped += 1
                log.info(f"    SKIP  {dest.name}")
            else:
                ok = download_file(file_url, dest)
                downloaded += 1 if ok else 0
                failed     += 0 if ok else 1

    log.info("")
    log.info("=" * 65)
    log.info("FINISHED")
    log.info(f"  Downloaded : {downloaded}")
    log.info(f"  Skipped    : {skipped}  (already in collection)")
    log.info(f"  Failed     : {failed}")
    log.info(f"  Log        : {log_path}")
    log.info("=" * 65)

    if failed:
        log.info("  Tip: re-run to retry any failed downloads.")

    if downloaded > 0:
        log.info("")
        log.info("  Next: run generate_psd_data.py → reload psd_dashboard.html")


if __name__ == "__main__":
    main()
