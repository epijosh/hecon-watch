"""
extract_psd_text.py
━━━━━━━━━━━━━━━━━━
Extracts structured data from PBAC Public Summary Documents using Claude Haiku.

For each PDF in this folder:
  1. Pulls text with pdfplumber (first 5 pages — enough for the key fields)
  2. Sends to Claude Haiku with a structured extraction prompt
  3. Gets back JSON: recommendation, ICER, comparator, indication, etc.
  4. Saves to psd_extracted.csv

SETUP:
  pip install pdfplumber anthropic python-dotenv --break-system-packages

  Create a .env file in this folder:
    ANTHROPIC_API_KEY=sk-ant-api03-...

USAGE:
  python extract_psd_text.py              # process all PSDs
  python extract_psd_text.py --limit 5   # test with first 5 (recommended first run)
  python extract_psd_text.py --resume    # skip already-processed files
  python extract_psd_text.py --delay 1   # slower if hitting rate limits

COST (rough estimate):
  ~1,600 PSDs × ~2,500 tokens input + 1,024 output ≈ $10–14 total (Haiku pricing)

OUTPUT:
  psd_extracted.csv — one row per PSD with structured PBAC fields
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

# ── Try loading .env ──────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(HERE / ".env")
except ImportError:
    pass  # python-dotenv optional; key can come from environment directly

# ── Dependencies check ────────────────────────────────────────────────────────
try:
    import pdfplumber
except ImportError:
    print("Missing dependency. Run:\n  pip install pdfplumber anthropic python-dotenv --break-system-packages")
    sys.exit(1)

try:
    import anthropic
except ImportError:
    print("Missing dependency. Run:\n  pip install anthropic --break-system-packages")
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
  "resubmission": <true if described as a resubmission, re-application, or major re-submission, else false>
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


def truncate(text: str, max_chars: int = 9_000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[... document truncated for extraction ...]"


# ── Claude extraction ─────────────────────────────────────────────────────────

def call_claude(client: anthropic.Anthropic, text: str, delay: float) -> dict:
    """Send text to Claude Haiku and parse JSON response."""
    if len(text.strip()) < 80:
        return {"extraction_ok": False, "error_note": "PDF yielded no usable text"}

    prompt = USER_PROMPT_TEMPLATE.format(text=truncate(text))

    retries = 0
    while retries <= 4:
        try:
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
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
    """Extract year and month from PSD filename like 'drug-psd-march-2023.pdf'."""
    MONTHS = {
        "jan": "1", "feb": "2", "mar": "3", "apr": "4", "may": "5", "jun": "6",
        "jul": "7", "aug": "8", "sep": "9", "oct": "10", "nov": "11", "dec": "12",
    }
    m = re.search(r'-psd-([a-z]+)-(\d{4})\.pdf$', filename.lower())
    if m:
        mon = MONTHS.get(m.group(1)[:3], "")
        return m.group(2), mon
    # Alternate pattern: drug-psd-03-2023.pdf
    m2 = re.search(r'-psd-(\d{1,2})-(\d{4})\.pdf$', filename.lower())
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

    # Find PSD PDFs
    PSD_RE = re.compile(r'.+-psd-.+\.pdf$', re.IGNORECASE)
    all_pdfs = sorted(p for p in HERE.glob("*.pdf") if PSD_RE.match(p.name))

    if not all_pdfs:
        print("No PSD PDF files found in this folder.")
        sys.exit(1)

    output_path = HERE / "psd_extracted.csv"
    done = load_done(output_path) if args.resume else set()
    queue = [p for p in all_pdfs if p.name not in done]
    if args.limit:
        queue = queue[: args.limit]

    print("=" * 62)
    print("PBAC PSD Extractor — Claude Haiku")
    print("=" * 62)
    print(f"  PSDs found       : {len(all_pdfs):,}")
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

        for i, pdf_path in enumerate(queue, 1):
            pct = i / len(queue) * 100
            short_name = pdf_path.name[:52]
            print(f"  [{i:4d}/{len(queue)}] {pct:5.1f}%  {short_name:<52}", end="", flush=True)

            text = extract_pdf_text(pdf_path)
            data = call_claude(client, text, args.delay)
            write_row(writer, pdf_path.name, data)
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
