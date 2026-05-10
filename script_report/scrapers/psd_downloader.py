"""
download_missing_psds.py
━━━━━━━━━━━━━━━━━━━━━━━━
Polite, idempotent downloader of every PBAC Public Summary Document listed at
  https://www.pbs.gov.au/info/industry/listing/elements/pbac-meetings/psd/public-summary-documents-by-product

Crawls the master "by product" index (and any A–Z / pagination sub-pages it links
to), figures out which PSDs aren't already on disk, and downloads only those.

Idempotent — re-run any time. Safe to interrupt.

POLITE SCRAPING RULES
  - 1.5 s delay between every request
  - Honest User-Agent
  - HEAD before GET when guessing file URLs
  - URL dedup so each page is fetched once

REQUIREMENTS
  pip install requests beautifulsoup4

USAGE
  python download_missing_psds.py
"""

from __future__ import annotations

import sys
import re
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
from script_report.config import REPO_ROOT, DATA_DIR, PSDS_DIR
from script_report.utils.helpers import MONTH_ABBR, MONTH_NUM

HERE       = REPO_ROOT
OUTPUT_DIR = PSDS_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Folders to scan for PSDs you already have (so we never re-download)
EXISTING_DIRS = [HERE, DATA_DIR, PSDS_DIR]

REQUEST_DELAY = 1.5    # seconds between every request
TIMEOUT       = 30

PBS_BASE  = "https://www.pbs.gov.au"
PBS_MEDIA = "https://m.pbs.gov.au"   # actual PDF/DOCX files live here

START_PAGES = [
    "/info/industry/listing/elements/pbac-meetings/psd/public-summary-documents-by-product",
    # Fallback URL variants in case the redesign moved things
    "/pbs/industry/listing/elements/pbac-meetings/psd/public-summary-documents-by-product",
    "/info/industry/listing/elements/pbac-meetings/psd",
]

# Regex for an individual PSD HTML page URL: /psd/2024-11/<slug>
PSD_PAGE_RE = re.compile(r"/psd/(\d{4}-\d{2})/([^/?#]+)/?$", re.IGNORECASE)

# Sub-index pages worth following from the master index
SUB_INDEX_HINTS = (
    "public-summary-documents-by-product",
    "psd-by-product",
    "pbac-meetings/psd",
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; PBAC-PSD-Archiver/2.0; "
        "personal research archive; +https://www.pbs.gov.au)"
    ),
    "Accept":          "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}

# ── logging ───────────────────────────────────────────────────────────────────
log_path = DATA_DIR / "download_log.txt"
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
_fetched: set[str] = set()


def polite_get(url: str, stream: bool = False) -> requests.Response | None:
    if not stream and url in _fetched:
        return None
    _fetched.add(url)
    time.sleep(REQUEST_DELAY)
    try:
        r = session.get(url, timeout=TIMEOUT, stream=stream)
        r.raise_for_status()
        return r
    except requests.RequestException as e:
        log.warning(f"  GET failed: {url}")
        log.warning(f"           → {e}")
        return None


def polite_head(url: str) -> int:
    time.sleep(REQUEST_DELAY)
    try:
        r = session.head(url, timeout=10, allow_redirects=True)
        return r.status_code
    except requests.RequestException:
        return 0


# ── fingerprint helpers ───────────────────────────────────────────────────────
# A fingerprint is a tuple (drug-slug, year, month-num) used to compare local
# PSDs against remote ones, regardless of filename casing or month format.

FINGERPRINT_PATTERNS = [
    # name-psd-march-2018  /  name-psd-mar-2018
    re.compile(r"^(?P<drug>.+?)[-_]psd[-_](?P<month>[a-z]{3,9})[-_](?P<year>\d{4})$", re.I),
    # name-psd-03-2018
    re.compile(r"^(?P<drug>.+?)[-_]psd[-_](?P<month>\d{1,2})[-_](?P<year>\d{4})$", re.I),
    # legacy: drug_BRAND_etc_PSD_YYYY-MM_FINAL or drug_BRAND_PSD_n-m_YYYY-MM
    re.compile(r"^(?P<drug>[^_]+).*?_PSD_.*?(?P<year>\d{4})[-_](?P<month>\d{1,2})", re.I),
]


def fingerprint(stem: str) -> tuple[str, str, str] | None:
    s = unquote(stem).lower()
    s = re.sub(r"\.docx?$", "", s)              # drop legacy .docx.pdf etc
    s = s.replace("%20", "-").replace(" ", "-")
    s = re.sub(r"-+", "-", s)
    for pat in FINGERPRINT_PATTERNS:
        m = pat.match(s)
        if not m:
            continue
        drug  = re.sub(r"[^a-z0-9]+", "", m.group("drug"))
        year  = m.group("year")
        mraw  = m.group("month").lower()
        if mraw.isdigit():
            mnum = mraw.zfill(2)
        elif mraw[:3] in MONTH_NUM:
            mnum = MONTH_NUM[mraw[:3]]
        else:
            continue
        if drug and year and mnum:
            return (drug, year, mnum)
    return None


def fingerprint_url(psd_page_url: str) -> tuple[str, str, str] | None:
    """Compute a fingerprint from a master-index URL like
       /psd/2024-11/enzalutamide-PSD-November-2024 → (enzalutamide, 2024, 11)."""
    m = PSD_PAGE_RE.search(psd_page_url)
    if not m:
        return None
    meeting, slug = m.group(1), m.group(2)
    fp = fingerprint(Path(slug).stem)
    if fp:
        return fp
    # Fallback: take meeting + a sanitised drug guess from the slug
    drug = slug.lower().split("-psd-")[0]
    drug = re.sub(r"[^a-z0-9]+", "", drug)
    if drug:
        y, mo = meeting.split("-")
        return (drug, y, mo)
    return None


def fingerprint_file_url(file_url: str) -> tuple[str, str, str] | None:
    name = Path(unquote(urlparse(file_url).path)).stem
    return fingerprint(name)


# ── existing-file index ───────────────────────────────────────────────────────
def index_existing() -> set[tuple[str, str, str]]:
    have: set[tuple[str, str, str]] = set()
    files = 0
    for d in EXISTING_DIRS:
        if not d.exists():
            continue
        for ext in ("*.pdf", "*.docx", "*.html"):
            for p in d.glob(ext):
                files += 1
                fp = fingerprint(p.stem)
                if fp:
                    have.add(fp)
    log.info(f"  Local PSDs on disk: {files} files → {len(have)} unique fingerprints")
    return have


# ── crawl ────────────────────────────────────────────────────────────────────
def looks_like_subindex(url: str) -> bool:
    p = url.lower()
    return any(h in p for h in SUB_INDEX_HINTS)


def crawl_for_psd_links(start_urls: list[str]) -> tuple[set[str], set[str]]:
    """
    Returns (psd_pages, direct_files). Walks the master index plus any pages
    it links to that look like sub-indexes (A–Z, pagination, etc.).
    """
    queue: list[str] = list(start_urls)
    visited: set[str] = set()
    psd_pages: set[str] = set()
    direct_files: set[str] = set()

    while queue:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        log.info(f"  Crawling: {url}")
        resp = polite_get(url)
        if not resp:
            continue
        soup = BeautifulSoup(resp.text, "html.parser")

        for a in soup.find_all("a", href=True):
            full = urljoin(url, a["href"]).split("#")[0].strip()
            if not full:
                continue

            low = full.lower()
            host = urlparse(full).netloc.lower()
            if host and not (host.endswith("pbs.gov.au")):
                continue

            # Direct PDF/DOCX file (m.pbs.gov.au or older pbs.gov.au paths)
            if low.endswith(".pdf") or low.endswith(".docx"):
                if "/psd/" in low or "/pbac-meetings/" in low:
                    direct_files.add(full)
                continue

            # Per-PSD HTML page
            if PSD_PAGE_RE.search(full):
                psd_pages.add(full)
                continue

            # Sub-index page worth following
            if looks_like_subindex(full) and full not in visited:
                queue.append(full)

        log.info(f"    cumulative: {len(psd_pages)} PSD pages, {len(direct_files)} direct files")

    return psd_pages, direct_files


# ── per-PSD: locate the file URL ──────────────────────────────────────────────
def get_file_url_from_page(psd_page_url: str) -> tuple[str | None, str | None]:
    """Returns (file_url, html_text). Either may be None on fetch failure.
    The html_text is returned so the HTML-fallback path doesn't have to re-fetch."""
    resp = polite_get(psd_page_url)
    if not resp:
        return None, None
    soup = BeautifulSoup(resp.text, "html.parser")
    pdf = docx = None
    for a in soup.find_all("a", href=True):
        full = urljoin(psd_page_url, a["href"])
        low = full.lower()
        if low.endswith(".pdf") and not pdf:
            pdf = full
        if low.endswith(".docx") and not docx:
            docx = full
    return (pdf or docx), resp.text


def guess_file_url(psd_page_url: str) -> str | None:
    """Construct an m.pbs.gov.au file URL from a per-PSD page URL.

    Pattern observed:
      page: /info/.../psd/2024-11/enzalutamide-PSD-November-2024
      file: m.pbs.gov.au/.../psd/2024-11/files/enzalutamide-psd-nov-2024.pdf
    """
    m = PSD_PAGE_RE.search(psd_page_url)
    if not m:
        return None
    meeting, slug = m.group(1), m.group(2)
    s = slug.lower()
    for full_m, abbr in MONTH_ABBR.items():
        s = s.replace(f"-{full_m}-", f"-{abbr}-")

    candidates: list[str] = []
    for stem in dict.fromkeys([s, slug.lower()]):
        for ext in (".pdf", ".docx"):
            candidates.append(
                f"{PBS_MEDIA}/industry/listing/elements/pbac-meetings/psd/{meeting}/files/{stem}{ext}"
            )
    for c in candidates:
        if polite_head(c) == 200:
            return c
    return None


# ── HTML PSD capture (for online-only PSDs with no PDF download) ──────────────
def derive_html_filename(psd_page_url: str) -> str | None:
    """Build a filename like 'enzalutamide-psd-nov-2024.html' from a page URL,
    matching the same naming pattern PDFs use so date parsers work unchanged."""
    m = PSD_PAGE_RE.search(psd_page_url)
    if not m:
        return None
    meeting, slug = m.group(1), m.group(2)
    s = slug.lower()
    for full_m, abbr in MONTH_ABBR.items():
        s = s.replace(f"-{full_m}-", f"-{abbr}-")
    # Normalise separators: 'enzalutamide-psd-nov-2024' style
    s = re.sub(r"[_\s]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if not re.search(r"-psd-", s):
        # Fall back: graft the meeting code on the end
        year, mo = meeting.split("-")
        mname = next((a for a in MONTH_NUM if MONTH_NUM[a] == mo), mo)
        s = f"{s}-psd-{mname}-{year}"
    return f"{s}.html"


def extract_main_html(html: str, source_url: str) -> str:
    """Strip nav/script/footer noise and keep the main PSD content. Returns a
    self-contained HTML snippet (with a comment recording the source URL)."""
    soup = BeautifulSoup(html, "html.parser")

    # Drop chrome
    for tag in soup(["script", "style", "nav", "header", "footer", "form",
                      "aside", "noscript", "iframe", "svg"]):
        tag.decompose()

    # Find the most likely main-content container.
    # PBS uses Drupal — order favours their selectors first.
    candidates = []
    for sel in ["main", "article", "[role=main]",
                ".layout-content", ".region-content", ".node__content",
                ".field--name-body", ".main-content", "#main", "#content",
                ".content"]:
        for el in soup.select(sel):
            txt = el.get_text(" ", strip=True)
            if len(txt) > 200:
                candidates.append((len(txt), el))
    candidates.sort(reverse=True, key=lambda x: x[0])
    body = candidates[0][1] if candidates else (soup.body or soup)

    return (
        f"<!-- captured by download_missing_psds.py from {source_url} -->\n"
        + str(body)
    )


def save_psd_html(psd_page_url: str, html: str | None = None) -> tuple[bool, str | None]:
    """Capture an HTML-only PSD. Returns (ok, dest_filename).

    If `html` is supplied (e.g. fetched earlier by get_file_url_from_page),
    we skip the network call and use it directly — avoids the polite_get
    dedup guard that would otherwise return None on a second fetch."""
    fname = derive_html_filename(psd_page_url)
    if not fname:
        return False, None

    dest = OUTPUT_DIR / fname
    if dest.exists():
        log.info(f"    SKIP  {dest.name}  (HTML already captured)")
        return True, fname

    if html is None:
        # Forced fetch — bypass dedup since this is the only way we'll get the page
        time.sleep(REQUEST_DELAY)
        try:
            r = session.get(psd_page_url, timeout=TIMEOUT)
            r.raise_for_status()
            html = r.text
        except requests.RequestException as e:
            log.warning(f"    HTML fetch failed: {psd_page_url}  → {e}")
            return False, None

    if not html or len(html) < 500:
        log.warning(f"    HTML response too short ({len(html or '')} chars): {psd_page_url}")
        return False, None

    log.info(f"    ↓     {fname}  (HTML PSD)")
    try:
        snippet = extract_main_html(html, psd_page_url)
        if len(snippet) < 500:
            log.warning(f"    HTML body extraction too small ({len(snippet)} chars), saving raw page")
            snippet = f"<!-- captured by download_missing_psds.py from {psd_page_url} -->\n{html}"
        dest.write_text(snippet, encoding="utf-8")
        log.info(f"    ✓     {dest.name}  ({dest.stat().st_size // 1024} KB)")
        return True, fname
    except Exception as e:
        log.error(f"    HTML write error: {e}")
        dest.unlink(missing_ok=True)
        return False, None


# ── download ──────────────────────────────────────────────────────────────────
def safe_filename(file_url: str) -> str:
    return unquote(urlparse(file_url).path.split("/")[-1])


def download_file(url: str, dest: Path) -> bool:
    log.info(f"    ↓     {dest.name}")
    resp = polite_get(url, stream=True)
    if not resp:
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
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
    log.info("PBAC PSD Downloader  —  full master-index sweep")
    log.info(f"Output: {OUTPUT_DIR}")
    log.info("=" * 65)
    log.info("")

    log.info("Step 1: Indexing local PSDs already on disk...")
    have = index_existing()
    log.info("")

    log.info("Step 2: Crawling master 'by product' index...")
    start_urls = [PBS_BASE + p for p in START_PAGES]
    psd_pages, direct_files = crawl_for_psd_links(start_urls)
    log.info(f"  Discovered: {len(psd_pages)} PSD pages, {len(direct_files)} direct file URLs")
    log.info("")

    if not psd_pages and not direct_files:
        log.warning("  ⚠  No PSDs discovered. The master index may now be JS-rendered.")
        log.warning("  Try the headless-browser fallback:")
        log.warning("     pip install playwright && python -m playwright install chromium")
        log.warning("  (then ask Claude to add a Playwright crawl path).")
        return

    # ── plan downloads (de-dup by fingerprint, skip what we already have) ────
    log.info("Step 3: Reconciling against local collection...")
    plan: list[tuple[tuple[str, str, str], str]] = []   # (fingerprint, file_url_or_page_url)
    seen_fps: set[tuple[str, str, str]] = set(have)

    # Direct file URLs first — fastest, fingerprint from filename
    for fu in sorted(direct_files):
        fp = fingerprint_file_url(fu)
        if not fp:
            continue
        if fp in seen_fps:
            continue
        seen_fps.add(fp)
        plan.append((fp, fu))

    # Then per-PSD HTML pages — fingerprint from URL slug
    for pu in sorted(psd_pages, key=lambda u: PSD_PAGE_RE.search(u).group(1) if PSD_PAGE_RE.search(u) else ""):
        fp = fingerprint_url(pu)
        if not fp:
            continue
        if fp in seen_fps:
            continue
        seen_fps.add(fp)
        plan.append((fp, pu))

    log.info(f"  Need to download: {len(plan)}")
    log.info(f"  Already have    : {len(have)}  (skipped on fingerprint match)")
    log.info("")

    if not plan:
        log.info("Collection is up to date. Nothing to download.")
        return

    log.info("Step 4: Downloading...")
    downloaded = downloaded_html = failed = 0

    for i, (fp, target) in enumerate(plan, 1):
        drug, year, mo = fp
        log.info(f"")
        log.info(f"[{i}/{len(plan)}]  {drug}  ·  {year}-{mo}")

        # Already an http(s) file URL?
        if target.lower().endswith((".pdf", ".docx")):
            file_url = target
            page_html = None
        else:
            file_url, page_html = get_file_url_from_page(target)
            if not file_url:
                file_url = guess_file_url(target)
            if not file_url:
                # No PDF/DOCX on this page — fall back to capturing the HTML
                log.info("    No PDF/DOCX on this page — capturing as HTML")
                ok, _ = save_psd_html(target, html=page_html)
                if ok:
                    downloaded_html += 1
                    have.add(fp)
                else:
                    failed += 1
                continue

        # Final guard: does the resolved filename already match something we have?
        fp2 = fingerprint_file_url(file_url)
        if fp2 and fp2 in have:
            log.info(f"    SKIP  (already on disk via different naming: {Path(safe_filename(file_url)).name})")
            continue

        dest = OUTPUT_DIR / safe_filename(file_url)
        if dest.exists():
            log.info(f"    SKIP  {dest.name}  (already in {OUTPUT_DIR.name}/)")
            continue

        if download_file(file_url, dest):
            downloaded += 1
            have.add(fp)
        else:
            failed += 1

    log.info("")
    log.info("=" * 65)
    log.info("FINISHED")
    log.info(f"  Downloaded PDFs/DOCX : {downloaded}")
    log.info(f"  Captured HTML PSDs   : {downloaded_html}")
    log.info(f"  Failed               : {failed}")
    log.info(f"  On disk total        : {len(have)} unique PSDs")
    log.info(f"  Saved into           : {OUTPUT_DIR}")
    log.info(f"  Log                  : {log_path}")
    log.info("=" * 65)

    if failed:
        log.info("  Tip: re-run to retry any failed downloads.")
    if downloaded > 0:
        log.info("")
        log.info("  Next: python build_site_data.py  →  refresh dashboard")


if __name__ == "__main__":
    main()
