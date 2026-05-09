"""
download_msac_outcomes.py
━━━━━━━━━━━━━━━━━━━━━━━━━
Scrapes MSAC (Medical Services Advisory Committee) application outcomes
and downloads Public Summary Documents from msac.gov.au.

MSAC is the medical services equivalent of PBAC — it evaluates procedures,
diagnostics, and devices for MBS (Medicare) funding. Combining MSAC + PBAC
data gives you the full picture of Australian HTA.

WHAT YOU GET:
  msac_outcomes.csv     — all applications with outcomes (supported/not/deferred)
  msac_psds/            — subfolder of downloaded PSDs

RATE LIMIT: 2 second delay between requests.

REQUIREMENTS:
  pip install requests beautifulsoup4

USAGE:
  python download_msac_outcomes.py

  Re-run at any time — skips already-downloaded files.
"""

from __future__ import annotations

import csv
import sys
import time
import json
import logging
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Run:  pip install requests beautifulsoup4")
    sys.exit(1)

# ── config ────────────────────────────────────────────────────────────────────
OUTPUT_DIR = Path(__file__).parent
PSD_DIR    = OUTPUT_DIR / "msac_psds"
PSD_DIR.mkdir(exist_ok=True)

MSAC_BASE  = "https://www.msac.gov.au"
DELAY      = 2.0   # seconds between requests

HEADERS = {
    "User-Agent": "PBAC-OpenData/1.0 (research archive; +https://github.com)",
    "Accept":     "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}

# ── logging ───────────────────────────────────────────────────────────────────
log_path = OUTPUT_DIR / "msac_download_log.txt"
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

session = requests.Session()
session.headers.update(HEADERS)
_seen: set = set()


def polite_get(url: str, stream: bool = False) -> requests.Response | None:
    if url in _seen:
        return None
    _seen.add(url)
    time.sleep(DELAY)
    try:
        r = session.get(url, timeout=30, stream=stream)
        r.raise_for_status()
        return r
    except requests.RequestException as e:
        log.error(f"  GET failed: {url} → {e}")
        return None


# ── scrape the applications listing ──────────────────────────────────────────
def get_application_links(page: int = 0) -> tuple[list[str], bool]:
    """
    Fetch one page of the MSAC applications listing.
    Returns (list of application URLs, has_more_pages).
    """
    # MSAC uses Drupal — try common pagination patterns
    url = f"{MSAC_BASE}/applications"
    params = {}
    if page > 0:
        params["page"] = page

    resp = polite_get(url + (f"?page={page}" if page > 0 else ""))
    if not resp:
        return [], False

    soup = BeautifulSoup(resp.text, "html.parser")
    links = []

    # Find links to individual application pages
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full = urljoin(MSAC_BASE, href)
        # Application pages typically have numeric IDs or specific path patterns
        if (
            "/applications/" in full
            and full != f"{MSAC_BASE}/applications"
            and not full.endswith("/applications/")
            and full not in links
        ):
            links.append(full)

    # Check for next page link
    has_more = bool(
        soup.find("a", {"rel": "next"})
        or soup.find("li", class_="pager__item--next")
        or soup.find("a", string=lambda t: t and "next" in t.lower())
    )

    return links, has_more


def scrape_all_application_links() -> list[str]:
    """Paginate through all MSAC applications and collect URLs."""
    all_links = []
    page = 0

    while True:
        log.info(f"  Fetching applications listing page {page}...")
        links, has_more = get_application_links(page)
        new_links = [l for l in links if l not in all_links]
        all_links.extend(new_links)
        log.info(f"  Page {page}: {len(links)} links found ({len(new_links)} new)")

        if not has_more or not new_links:
            break
        page += 1

        if page > 100:   # safety cap
            log.warning("  Hit page cap (100) — stopping pagination")
            break

    return all_links


# ── scrape an individual application page ────────────────────────────────────
def scrape_application(url: str) -> dict | None:
    """
    Visit one MSAC application page and extract:
    - Application number
    - Application name / topic
    - Applicant
    - Meeting date
    - Outcome (supported / not supported / deferred)
    - PSD link
    """
    resp = polite_get(url)
    if not resp:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    def find_text(selectors: list[str]) -> str:
        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                return el.get_text(strip=True)
        return ""

    def find_meta(label: str) -> str:
        """Find a field value from a definition list or table."""
        for tag in soup.find_all(["dt", "th", "td", "strong", "label"]):
            if label.lower() in tag.get_text(strip=True).lower():
                sib = tag.find_next_sibling()
                if sib:
                    return sib.get_text(strip=True)
                parent = tag.parent
                if parent:
                    nxt = parent.find_next_sibling()
                    if nxt:
                        return nxt.get_text(strip=True)
        return ""

    # Page title / application name
    title = find_text(["h1", ".field--name-title", ".page-title"]) or url.split("/")[-1]

    # Extract key metadata
    record = {
        "url":            url,
        "title":          title,
        "app_number":     find_meta("Application number") or find_meta("MSAC ref"),
        "applicant":      find_meta("Applicant") or find_meta("Sponsor"),
        "meeting_date":   find_meta("Meeting date") or find_meta("MSAC meeting"),
        "outcome":        find_meta("Outcome") or find_meta("Funding recommendation"),
        "application_type": find_meta("Application type") or find_meta("Type"),
        "psd_url":        None,
        "psd_filename":   None,
    }

    # Find PSD link
    pdf_url = docx_url = None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full = urljoin(url, href)
        text = a.get_text(strip=True).lower()
        low  = full.lower()
        # Prioritise links explicitly labelled as PSD
        if "public summary" in text or "psd" in text:
            if low.endswith(".pdf"):
                pdf_url = full
            elif low.endswith(".docx"):
                docx_url = full
        elif low.endswith(".pdf") and not pdf_url:
            pdf_url = full
        elif low.endswith(".docx") and not docx_url:
            docx_url = full

    record["psd_url"]      = pdf_url or docx_url
    record["psd_filename"] = unquote(urlparse(record["psd_url"]).path.split("/")[-1]) if record["psd_url"] else None

    return record


# ── download a PSD ────────────────────────────────────────────────────────────
def download_psd(url: str, dest: Path) -> bool:
    if dest.exists():
        log.info(f"    SKIP  {dest.name}")
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


# ── save outcomes CSV ─────────────────────────────────────────────────────────
def save_csv(records: list[dict]):
    path = OUTPUT_DIR / "msac_outcomes.csv"
    if not records:
        return
    fields = list(records[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(records)
    log.info(f"Saved outcomes CSV: {path}  ({len(records)} rows)")


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("MSAC Outcomes Scraper")
    log.info(f"Saving PSDs to: {PSD_DIR}")
    log.info("=" * 60)

    # Step 1: Discover all application URLs
    log.info("")
    log.info("Step 1: Finding all MSAC application pages...")
    app_urls = scrape_all_application_links()
    log.info(f"  Total application pages found: {len(app_urls)}")

    if not app_urls:
        log.warning("")
        log.warning("No application links found — MSAC site may be JavaScript-rendered.")
        log.warning("Visit https://www.msac.gov.au/applications manually to browse.")
        log.warning("If needed, ask Claude to update this script to use Playwright.")
        return

    # Step 2: Scrape each application and download PSDs
    log.info("")
    log.info("Step 2: Scraping application details and downloading PSDs...")

    records = []
    downloaded = skipped = failed = 0

    for i, url in enumerate(app_urls, 1):
        log.info(f"  [{i}/{len(app_urls)}] {url}")
        record = scrape_application(url)

        if not record:
            failed += 1
            continue

        records.append(record)

        if record["psd_url"] and record["psd_filename"]:
            dest = PSD_DIR / record["psd_filename"]
            ok = download_psd(record["psd_url"], dest)
            if ok:
                if dest.stat().st_size > 0 if dest.exists() else False:
                    downloaded += 1
                else:
                    skipped += 1
            else:
                failed += 1

        # Save incrementally every 50 records
        if i % 50 == 0:
            save_csv(records)
            log.info(f"  (checkpoint saved — {i} applications processed)")

    # Final save
    save_csv(records)

    log.info("")
    log.info("=" * 60)
    log.info("FINISHED")
    log.info(f"  Applications scraped : {len(records)}")
    log.info(f"  PSDs downloaded      : {downloaded}")
    log.info(f"  PSDs skipped         : {skipped} (already existed)")
    log.info(f"  Failed               : {failed}")
    log.info(f"  Outcomes CSV         : {OUTPUT_DIR / 'msac_outcomes.csv'}")
    log.info(f"  PSDs folder          : {PSD_DIR}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
