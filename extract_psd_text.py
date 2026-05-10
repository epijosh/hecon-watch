"""
extract_psd_text.py
━━━━━━━━━━━━━━━━━━
Extracts structured data from PBAC Public Summary Documents using Claude Haiku.

Handles both formats now:
  - PDF PSDs (the historic format) — text pulled via pdfplumber
  - HTML PSDs (the post-2024 online format, captured by download_missing_psds.py)
    — text pulled via BeautifulSoup

For each PSD in data/psds/:
  1. Pull text (first 5 pages of PDFs / cleaned HTML body)
  2. Send to Claude Haiku with a structured extraction prompt
  3. Receive JSON: recommendation, ICER, comparator, indication, etc.
  4. Append to data/psd_extracted.csv

SETUP:
  pip install pdfplumber anthropic python-dotenv beautifulsoup4 --break-system-packages

  Create a .env file in this folder:
    ANTHROPIC_API_KEY=sk-ant-api03-...

USAGE:
  python extract_psd_text.py              # process all PSDs
  python extract_psd_text.py --limit 5   # test with first 5 (recommended first run)
  python extract_psd_text.py --resume    # skip already-processed files
  python extract_psd_text.py --delay 1   # slower if hitting rate limits

COST (rough estimate):
  ~$0.006 per PSD with Haiku pricing — ~$10–14 for 1,600+ PSDs

OUTPUT:
  data/psd_extracted.csv — one row per PSD with structured PBAC fields
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

HERE = Path(__file__).parent
DATA = HERE / "data"          # CSVs written here; fall back to HERE for legacy
DATA.mkdir(exist_ok=True)
PSDS = DATA / "psds"          # PDFs live here after migration (or in HERE before)

def _pdf_dirs() -> list[Path]:
    """Folders to scan for PSD PDFs (psds/ first, then root fallback)."""
    dirs = []
    if PSDS.exists():
        dirs.append(PSDS)
    dirs.append(HERE)
    return dirs

# ── Try loading .env ──────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(HERE / ".env")
except ImportError:
    pass  # python-dotenv optional; key can come from environment directly

# ── Dependencies check ────────────────────────────────────────────────────────
try:
    import pdfplumber
    import anthropic
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependency. Run:\n  pip install pdfplumber anthropic python-dotenv beautifulsoup4 --break-system-packages")
    sys.exit(1)

# ── CSV output fields ─────────────────────────────────────────────────────────
FIELDS = [
    "filename",
    "drug",
    "brand_name",
    "indication",
    "therapy_area",
    "recommendation",
    "listing_type",
    "comparator",
    "icer_low",
    "icer_high",
    "icer_note",
    "risk_sharing",
    "risk_sharing_note",
    "population_per_year",
    "key_trials",
    "resubmission",
    # ── Deeper fields (added May 2026) ────────────────────────────────────────
    "budget_impact_aud",
    "rejection_reasons",
    "patient_advocacy",
    "pico_population",
    "evidence_type",
    "line_of_therapy",
    "trial_size",
    "primary_endpoint",
    "economic_model",
    # ─────────────────────────────────────────────────────────────────────────
    "pbac_year",
    "pbac_month",
    "extraction_ok",
    "error_note",
]

# ── Extraction prompt ─────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a precise data extractor for Australian PBAC (Pharmaceutical Benefits Advisory Committee) Public Summary Documents. Extract exactly the fields requested. Return only valid JSON."""

USER_PROMPT_TEMPLATE = """Extract structured data from this PBAC Public Summary Document (PSD) text.

Return ONLY a JSON object with these fields (use null for anything not found):

{{
  "drug": "generic/INN drug name",
  "brand_name": "trade/brand name if mentioned, else null",
  "indication": "the specific indication being assessed — concise, 1–2 sentences",
  "therapy_area": "one of: Oncology | Haematology | Rare disease | Immunology | Cardiovascular | Diabetes | Neurology | Respiratory | Musculo-skeletal | Ophthalmology | Infectious disease | Other",
  "recommendation": "one of: Recommended | Not recommended | Deferred | Noted | Recommended with restriction",
  "listing_type": "one of: Unrestricted | Restricted | Authority Required | Not applicable | null",
  "comparator": "the main comparator used in the economic model (e.g. 'best supportive care', 'docetaxel')",
  "icer_low": <integer — lower bound of ICER in AUD, null if not stated or redacted>,
  "icer_high": <integer — upper bound of ICER in AUD, null if not stated or redacted>,
  "icer_note": "brief note if ICER is commercially sensitive, redacted, or not calculated — else null",
  "risk_sharing": <true if a risk sharing arrangement or managed access agreement is mentioned, else false>,
  "risk_sharing_note": "brief description of the arrangement if risk_sharing is true, else null",
  "population_per_year": <integer — estimated eligible Australian patients per year, null if not stated>,
  "key_trials": "comma-separated trial names/identifiers (e.g. 'KEYNOTE-189, KEYNOTE-407'), null if none",
  "resubmission": <true if described as a resubmission, re-application, or major re-submission, else false>,
  "budget_impact_aud": <integer — estimated net annual budget impact in AUD (government perspective), null if not stated>,
  "rejection_reasons": "if not recommended: comma-separated list of the PBAC's main concerns (e.g. 'uncertain OS benefit, high ICER, immature survival data') — else null",
  "patient_advocacy": <true if patient advocacy group, consumer group, or patient organisation input is noted in the PSD, else false>,
  "pico_population": "brief description of the PICO population — who the drug is indicated for (e.g. 'adults with previously treated metastatic NSCLC with PD-L1 ≥50%')",
  "evidence_type": "one of: RCT | Single-arm | Registry | Cost-minimisation | Meta-analysis | Other",
  "line_of_therapy": "one of: First-line | Second-line | Later-line | Any | Not applicable | null",
  "trial_size": <integer — number of patients in the pivotal trial(s), null if not stated>,
  "primary_endpoint": "one of: OS | PFS | DFS | ORR | QoL | Surrogate | Cost-minimisation | Other | null",
  "economic_model": "one of: CUA | CEA | Cost-minimisation | BIA only | Not modelled | null"
}}

Return ONLY the JSON object. No markdown fences, no explanation, no extra text.

PSD TEXT:
{text}"""


# ── PDF text extraction ───────────────────────────────────────────────────────

def extract_pdf_text(pdf_path: Path, max_pages: int = 5) -> str:
    """Extract text from first N pages. Returns empty string on failure."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            parts = []
            for page in pdf.pages[:max_pages]:
                t = page.extract_text()
                if t:
                    parts.append(t.strip())
            return "\n\n".join(parts)
    except Exception:
        return ""


def extract_html_text(html_path: Path) -> str:
    """Extract clean text from an HTML PSD captured by download_missing_psds.py.
    The downloader already trims chrome, but we strip again defensively."""
    try:
        raw = html_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""

    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer",
                     "aside", "noscript", "iframe", "svg", "form"]):
        tag.decompose()

    # Preserve table structure by inserting tabs/newlines, then collapse the rest
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tr in soup.find_all("tr"):
        tr.append("\n")
    for td in soup.find_all(["td", "th"]):
        td.append("\t")

    text = soup.get_text("\n", strip=True)
    # Collapse runs of blank lines / repeated whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def extract_text(path: Path) -> str:
    """Dispatch by extension."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        return extract_pdf_text(path)
    if ext == ".html":
        return extract_html_text(path)
    return ""


def truncate(text: str, max_chars: int = 9_000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[... document truncated for extraction ...]"


# ── Claude extraction ─────────────────────────────────────────────────────────

def call_claude(client: anthropic.Anthropic, text: str) -> dict:
    """Send text to Claude Haiku and parse JSON response."""
    if len(text.strip()) < 80:
        return {"extraction_ok": False, "error_note": "Source yielded no usable text"}

    prompt = USER_PROMPT_TEMPLATE.format(text=truncate(text))

    retries = 0
    while retries <= 4:
        try:
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1536,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()
            # Strip accidental markdown fences
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            data = json.loads(raw)
            # Haiku occasionally returns a list instead of a dict — unwrap it
            if isinstance(data, list):
                data = data[0] if data and isinstance(data[0], dict) else {}
            if not isinstance(data, dict):
                return {"extraction_ok": False, "error_note": "Response was not a JSON object"}
            data["extraction_ok"] = True
            data["error_note"] = ""
            return data

        except anthropic.RateLimitError:
            wait = min(60, 5 * (2 ** retries))
            print(f" [rate limit — waiting {wait}s]", end="", flush=True)
            time.sleep(wait)
            retries += 1

        except json.JSONDecodeError as e:
            return {"extraction_ok": False, "error_note": f"JSON parse failed: {str(e)[:80]}"}

        except anthropic.APIError as e:
            return {"extraction_ok": False, "error_note": f"API error: {str(e)[:120]}"}

    return {"extraction_ok": False, "error_note": "Rate limit — max retries exceeded"}


# ── CSV helpers ───────────────────────────────────────────────────────────────

def load_done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with open(path, encoding="utf-8") as f:
        return {row["filename"] for row in csv.DictReader(f) if row.get("filename")}


def parse_psd_date(filename: str) -> tuple[str, str]:
    """Extract year and month from a PSD filename like 'drug-psd-march-2023.pdf'
    or 'drug-psd-nov-2024.html'."""
    MONTHS = {
        "jan": "1", "feb": "2", "mar": "3", "apr": "4", "may": "5", "jun": "6",
        "jul": "7", "aug": "8", "sep": "9", "oct": "10", "nov": "11", "dec": "12",
    }
    m = re.search(r'-psd-([a-z]+)-(\d{4})\.(?:pdf|html)$', filename.lower())
    if m:
        mon = MONTHS.get(m.group(1)[:3], "")
        return m.group(2), mon
    # Alternate pattern: drug-psd-03-2023.pdf
    m2 = re.search(r'-psd-(\d{1,2})-(\d{4})\.(?:pdf|html)$', filename.lower())
    if m2:
        return m2.group(2), m2.group(1).lstrip("0")
    return "", ""


def serialise(val) -> str:
    if val is None:
        return ""
    if isinstance(val, bool):
        return "yes" if val else "no"
    if isinstance(val, (int, float)):
        return str(int(val)) if float(val) == int(val) else str(val)
    return str(val)


def write_row(writer: csv.DictWriter, filename: str, data: dict):
    yr, mo = parse_psd_date(filename)
    row = {"filename": filename, "pbac_year": yr, "pbac_month": mo}
    for field in FIELDS:
        if field in ("filename", "pbac_year", "pbac_month"):
            continue
        row[field] = serialise(data.get(field, ""))
    writer.writerow(row)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Extract structured data from PBAC PSDs via Claude Haiku")
    parser.add_argument("--limit",  type=int,   default=0,   help="Process only first N PDFs (0 = all)")
    parser.add_argument("--resume", action="store_true",     help="Skip already-processed files")
    parser.add_argument("--delay",  type=float, default=0.5, help="Seconds to pause between API calls")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key or not api_key.startswith("sk-"):
        print("ERROR: ANTHROPIC_API_KEY not set or looks wrong.")
        print("Create a .env file containing:  ANTHROPIC_API_KEY=sk-ant-api03-...")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    # Find PSD files — PDFs and HTML pages (the latter for online-only PSDs).
    # Check data/psds/ first, then root for legacy installs.
    PSD_RE = re.compile(r'.+-psd-.+\.(?:pdf|html)$', re.IGNORECASE)
    all_files: list[Path] = []
    for psd_dir in _pdf_dirs():
        found = []
        for ext in ("*.pdf", "*.html"):
            found.extend(p for p in psd_dir.glob(ext) if PSD_RE.match(p.name))
        all_files.extend(sorted(found))
    # Deduplicate by filename (psds/ takes priority)
    seen_names: set[str] = set()
    unique_files: list[Path] = []
    for p in all_files:
        if p.name not in seen_names:
            seen_names.add(p.name)
            unique_files.append(p)
    all_files = unique_files

    if not all_files:
        print(f"No PSD files found. Expected .pdf or .html in {PSDS} or {HERE}.")
        sys.exit(1)
    pdf_count = sum(1 for p in all_files if p.suffix.lower() == ".pdf")
    html_count = sum(1 for p in all_files if p.suffix.lower() == ".html")

    output_path = DATA / "psd_extracted.csv"
    done = load_done(output_path) if args.resume else set()
    queue = [p for p in all_files if p.name not in done]
    if args.limit:
        queue = queue[: args.limit]

    print("=" * 62)
    print("PBAC PSD Extractor — Claude Haiku")
    print("=" * 62)
    print(f"  PSDs found       : {len(all_files):,}  ({pdf_count} PDF · {html_count} HTML)")
    print(f"  Already done     : {len(done):,}")
    print(f"  To process       : {len(queue):,}")
    if args.limit:
        print(f"  Limit            : {args.limit}")
    est_cost = len(queue) * 0.006  # rough ~$0.006 per PSD
    print(f"  Est. cost        : ~${est_cost:.2f}")
    print(f"  Output           : psd_extracted.csv")
    print()

    if not queue:
        print("Nothing to process. Use --resume to add new files.")
        return

    mode = "a" if args.resume and output_path.exists() else "w"
    ok_count = fail_count = 0

    with open(output_path, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if mode == "w":
            writer.writeheader()

        for i, psd_path in enumerate(queue, 1):
            pct = i / len(queue) * 100
            kind = "HTML" if psd_path.suffix.lower() == ".html" else "PDF "
            short_name = psd_path.name[:48]
            print(f"  [{i:4d}/{len(queue)}] {pct:5.1f}% {kind} {short_name:<48}", end="", flush=True)

            text = extract_text(psd_path)
            data = call_claude(client, text)
            write_row(writer, psd_path.name, data)
            f.flush()

            if data.get("extraction_ok"):
                ok_count += 1
                rec = data.get("recommendation") or "?"
                icer_l = data.get("icer_low")
                icer_h = data.get("icer_high")
                icer_str = ""
                if icer_l and icer_h:
                    icer_str = f"  ICER ${icer_l/1000:.0f}k–${icer_h/1000:.0f}k"
                elif icer_l:
                    icer_str = f"  ICER ~${icer_l/1000:.0f}k"
                print(f"  ✓  {rec}{icer_str}")
            else:
                fail_count += 1
                err = (data.get("error_note") or "")[:45]
                print(f"  ✗  {err}")

            time.sleep(args.delay)

    print()
    print("=" * 62)
    print(f"  Extracted  ✓ {ok_count:,}   Failed ✗ {fail_count:,}")
    print(f"  Saved to   psd_extracted.csv")
    print()
    print("Next steps:")
    print("  1. Review psd_extracted.csv to check quality")
    print("  2. Run build_site_data.py to load it into the site")


if __name__ == "__main__":
    main()
