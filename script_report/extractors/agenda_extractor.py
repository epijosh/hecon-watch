"""
agenda_extractor.py
━━━━━━━━━━━━━━━━━━
Pulls upcoming PBAC + Intracycle meeting agendas off pbs.gov.au and turns them
into structured JSON for the dashboard.

PBAC publishes agendas ~6 weeks before each meeting. There are typically only
two ahead at any time (the next main PBAC meeting and the next Intracycle).
Past meetings are not interesting here — once the meeting happens its content
flows through to the PSDs pipeline and the dashboard already knows about them.

Source list lives at ``data/agenda_sources.json`` — a hand-curated list of
URLs. Each URL can be either the direct PDF or the HTML wrapper page (the
extractor walks the wrapper to find the PDF link). New agendas are added by
pasting their URL into that file; old ones become harmless once the meeting
date passes (the loader filters them out).

USAGE:
    python -m script_report agendas              # extract anything new
    python -m script_report agendas --re-extract # force re-extraction of all
    python -m script_report agendas --limit 1    # process only the first source

OUTPUT:
    data/pbac_agendas.json   — structured agenda items (one record per source URL)
    data/agendas/*.pdf       — downloaded source PDFs (gitignored, regenerable)

COST:
    ~$0.001 per agenda PDF with Haiku — trivial.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from script_report.config import DATA_DIR, HAIKU_MODEL
from script_report.utils.helpers import MONTH_MAP, load_dotenv_safely

load_dotenv_safely()

AGENDAS_DIR = DATA_DIR / "agendas"
SOURCE_LIST = DATA_DIR / "agenda_sources.json"
OUTPUT_JSON = DATA_DIR / "pbac_agendas.json"

USER_AGENT = "script.report agenda-fetcher (+https://script.report)"


try:
    import pdfplumber
    import anthropic
except ImportError:
    print("Missing dependency. Run:\n  pip install pdfplumber anthropic --break-system-packages")
    sys.exit(1)


# ── Source list ──────────────────────────────────────────────────────────────

def load_source_list() -> list[str]:
    if not SOURCE_LIST.exists():
        print(f"  No source list found at {SOURCE_LIST}.")
        print("  Create it with: { \"sources\": [\"<agenda-url>\", ...] }")
        return []
    try:
        data = json.loads(SOURCE_LIST.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"  agenda_sources.json is not valid JSON: {e}")
        return []
    return [u for u in data.get("sources", []) if isinstance(u, str) and u.strip()]


# ── URL resolution + download ────────────────────────────────────────────────

PDF_HREF_RE = re.compile(r'href=["\']([^"\']*\.pdf[^"\']*)["\']', re.IGNORECASE)


def _http_get(url: str, *, accept: str = "*/*") -> bytes:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urlopen(req, timeout=30) as resp:
        return resp.read()


def resolve_to_pdf_url(source_url: str) -> str | None:
    """If source_url is a direct PDF, return it. Otherwise fetch the HTML
    wrapper and find the first agenda-looking PDF link."""
    if source_url.lower().endswith(".pdf"):
        return source_url
    try:
        html = _http_get(source_url, accept="text/html").decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"    [warn] could not fetch wrapper {source_url} ({e})")
        return None
    # Pick the first PDF link that lives under the PBAC agendas path. Falls
    # back to any PDF if no agenda-shaped one is found.
    candidates = PDF_HREF_RE.findall(html)
    if not candidates:
        print(f"    [warn] no PDF link found in {source_url}")
        return None
    preferred = [h for h in candidates if "agenda" in h.lower() or "/pbac-meetings/" in h.lower()]
    href = (preferred or candidates)[0]
    return urljoin(source_url, href)


def download_pdf(pdf_url: str) -> Path | None:
    """Save the PDF to data/agendas/<filename>. Returns path or None on failure.
    Idempotent: re-downloads each call (PBAC may publish v2 / v3 of the same
    agenda; we want the latest)."""
    AGENDAS_DIR.mkdir(parents=True, exist_ok=True)
    filename = pdf_url.rsplit("/", 1)[-1]
    # URL-decode percent-escapes so the filename is human-readable
    from urllib.parse import unquote
    filename = unquote(filename)
    target = AGENDAS_DIR / filename
    try:
        body = _http_get(pdf_url, accept="application/pdf")
    except Exception as e:
        print(f"    [warn] download failed: {pdf_url} ({e})")
        return None
    target.write_bytes(body)
    return target


# ── PDF text extraction ──────────────────────────────────────────────────────

def extract_pdf_text(pdf_path: Path) -> str:
    """Pull text from every page. Agendas are short (3–8 pages); no truncation."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            parts = []
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    parts.append(t.strip())
            return "\n\n".join(parts)
    except Exception as e:
        print(f"    [warn] pdfplumber failed on {pdf_path.name}: {e}")
        return ""


# ── Meeting metadata heuristics ──────────────────────────────────────────────

_MEETING_LABEL_RE = re.compile(
    r"(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(?P<year>\d{4})\s+PBAC\s+(?:Intracycle\s+)?Meeting",
    re.IGNORECASE,
)


def guess_meeting(filename: str, text: str) -> tuple[str | None, str | None, str]:
    """Return (meeting_label, meeting_date_iso, meeting_kind).

    meeting_kind ∈ {"PBAC", "Intracycle"}. We default to PBAC unless the
    filename or first-page heading says Intracycle.
    """
    haystack = f"{filename}\n{text[:2000]}"
    is_intracycle = bool(re.search(r"intracycle", haystack, re.IGNORECASE))
    kind = "Intracycle" if is_intracycle else "PBAC"

    label = None
    iso_date = None
    m = _MEETING_LABEL_RE.search(haystack)
    if m:
        month_name = m.group("month").title()
        year = int(m.group("year"))
        label = f"{month_name} {year} PBAC{' Intracycle' if is_intracycle else ''} Meeting"
        # Approximate the meeting date — first day of the named month. The
        # exact day comes from the cycle calendar elsewhere; this is just a
        # sortable key for "is this in the future?" filtering.
        mon_int = MONTH_MAP.get(month_name[:3].lower())
        if mon_int:
            iso_date = f"{year:04d}-{mon_int:02d}-01"

    return label, iso_date, kind


# ── Haiku extraction ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a precise data extractor for Australian PBAC meeting agendas. "
    "Extract every agenda item. Return only valid JSON."
)

USER_PROMPT_TEMPLATE = """Extract every agenda item from this PBAC meeting agenda.

Each item is a row in the agenda table with three columns:
  1. Drug name + form/strength + sponsor + submission type
  2. Drug type / use (the indication)
  3. Listing requested / purpose of submission

Return ONLY a JSON object of this shape (no markdown fences, no extra text):

{{
  "items": [
    {{
      "drug": "primary generic drug name(s) — uppercase, the heading line of the row (e.g. 'TUCATINIB', 'ADALIMUMAB and INFLIXIMAB')",
      "trade_name": "trade/brand name if shown (e.g. 'TUKYSA®', 'Galafold®'), else null",
      "sponsor": "sponsor / applicant company exactly as shown (e.g. 'Pfizer Australia Pty Ltd', 'AMICUS THERAPEUTICS PTY LTD'), else null. If 'Various brands and sponsors' write that.",
      "indication": "concise indication / drug-type-and-use phrase from column 2 (e.g. 'Crohn disease', 'Fabry disease', 'Breast cancer'). Strip parenthetical abbreviations only if redundant. Write 'Not applicable' if the agenda says so.",
      "submission_category": "one of: Major submission | Minor submission | Resubmission | Internal submission | Stakeholder meeting | Post-market review | Matter arising | Other matter | Other. Pick from the parenthetical type marker on the row (e.g. '(Change to existing listing)' → 'Internal submission'; 'Matters outstanding' → 'Matter arising'; 'Post-market review' → 'Post-market review').",
      "submission_pathway": "one of: Standard Re-entry | Early Re-entry | Early Resolution | Facilitated Resolution | New listing | Change to existing listing | New NIP listing | Pricing matter | PBS review | Other matters | null. Use the parenthetical descriptor exactly when it matches one of these.",
      "purpose": "1-sentence summary of column 3 (purpose of submission). Trim to ~140 chars max."
    }}
  ]
}}

Rules:
- Skip the header row(s) and the introductory page.
- Do not invent fields. Use null if unsure.
- Order items as they appear in the agenda.
- If a row covers multiple drugs (e.g. 'ADALIMUMAB and INFLIXIMAB'), keep them combined in one item — do not split.

AGENDA TEXT:
{text}
"""


def call_haiku(client: anthropic.Anthropic, text: str) -> dict:
    if len(text.strip()) < 80:
        return {"items": [], "error": "no usable text"}

    prompt = USER_PROMPT_TEMPLATE.format(text=text[:18_000])
    retries = 0
    while retries <= 4:
        try:
            msg = client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                return {"items": [], "error": "response shape unexpected"}
            return data
        except anthropic.RateLimitError:
            wait = min(60, 5 * (2 ** retries))
            print(f"    [rate limit — waiting {wait}s]", end="", flush=True)
            time.sleep(wait)
            retries += 1
        except json.JSONDecodeError as e:
            return {"items": [], "error": f"JSON parse failed: {str(e)[:80]}"}
        except anthropic.APIError as e:
            return {"items": [], "error": f"API error: {str(e)[:120]}"}
    return {"items": [], "error": "rate-limit retries exhausted"}


# ── Output cache ─────────────────────────────────────────────────────────────

def load_cached() -> dict:
    if not OUTPUT_JSON.exists():
        return {"agendas": [], "last_updated": None}
    try:
        return json.loads(OUTPUT_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"  [warn] {OUTPUT_JSON.name} is corrupt — starting fresh")
        return {"agendas": [], "last_updated": None}


def write_output(payload: dict) -> None:
    payload["last_updated"] = date.today().isoformat()
    OUTPUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Extract PBAC + Intracycle meeting agendas")
    ap.add_argument("--re-extract", action="store_true",
                    help="Re-extract every source URL even if cached.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process only the first N sources (for testing).")
    args = ap.parse_args()

    sources = load_source_list()
    if args.limit:
        sources = sources[: args.limit]
    if not sources:
        print("Nothing to do — no sources in agenda_sources.json.")
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key.startswith("sk-"):
        print("ERROR: ANTHROPIC_API_KEY not set. Add it to .env.")
        sys.exit(1)
    client = anthropic.Anthropic(api_key=api_key)

    cached = load_cached()
    by_url = {a["source_url"]: a for a in cached.get("agendas", [])}

    print("=" * 62)
    print(f"PBAC Agenda Extractor — {len(sources)} source URL(s)")
    print("=" * 62)

    for i, source_url in enumerate(sources, 1):
        print(f"\n  [{i}/{len(sources)}] {source_url}")
        existing = by_url.get(source_url)

        pdf_url = resolve_to_pdf_url(source_url)
        if not pdf_url:
            print("    skipped (no PDF found)")
            continue
        print(f"    PDF: {pdf_url}")

        pdf_path = download_pdf(pdf_url)
        if not pdf_path:
            continue
        digest = file_sha256(pdf_path)

        if existing and not args.re_extract and existing.get("pdf_sha256") == digest:
            print(f"    cached ({len(existing.get('items', []))} items)")
            continue

        text = extract_pdf_text(pdf_path)
        label, mdate, kind = guess_meeting(pdf_path.name, text)
        print(f"    {label or '(no label)'} · {mdate or '(no date)'} · {kind}")

        result = call_haiku(client, text)
        if result.get("error"):
            print(f"    extraction failed: {result['error']}")
            continue
        items = result.get("items", [])
        print(f"    extracted {len(items)} items")

        by_url[source_url] = {
            "source_url":     source_url,
            "pdf_url":        pdf_url,
            "pdf_filename":   pdf_path.name,
            "pdf_sha256":     digest,
            "meeting_label":  label,
            "meeting_date":   mdate,
            "meeting_kind":   kind,
            "items":          items,
            "extracted_at":   date.today().isoformat(),
        }
        time.sleep(0.4)

    # Preserve original source order (stale entries fall to the end)
    in_order = [by_url[u] for u in sources if u in by_url]
    in_order += [a for u, a in by_url.items() if u not in sources]
    write_output({"agendas": in_order})

    upcoming = [a for a in in_order if a.get("meeting_date") and a["meeting_date"] >= date.today().isoformat()]
    print()
    print(f"  Total cached    : {len(in_order)}")
    print(f"  Upcoming        : {len(upcoming)}")
    print(f"  Output          : {OUTPUT_JSON.name}")
    print()
    print("Next: python -m script_report build")


if __name__ == "__main__":
    main()
