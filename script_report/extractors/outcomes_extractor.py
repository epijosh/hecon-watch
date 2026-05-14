"""
outcomes_extractor.py
━━━━━━━━━━━━━━━━━━━━━
Pulls the short "Recommendations made by the PBAC – <Month Year>" outcomes
PDFs off pbs.gov.au and turns them into structured JSON for the dashboard.

PBAC publishes outcomes ~6 weeks after each meeting; full PSDs follow several
weeks later. This bridges the gap: once a meeting happens but before its PSDs
land, the homepage can already show drug + indication + outcome from the
outcomes summary. Once the PSDs for that meeting are extracted, the loader
filters the meeting out (the PSDs carry richer detail).

Source list lives at ``data/outcomes_sources.json`` — a hand-curated list of
URLs. Each URL can be either the direct PDF or the HTML wrapper page (the
extractor walks the wrapper to find the PDF link, so the wrapper auto-resolves
to whatever version PBAC last published).

USAGE:
    python -m script_report outcomes              # extract anything new
    python -m script_report outcomes --re-extract # force re-extraction of all
    python -m script_report outcomes --limit 1    # process only the first source

OUTPUT:
    data/pbac_outcomes.json   — structured outcome items (one record per meeting)
    data/outcomes/*.pdf       — downloaded source PDFs (gitignored, regenerable)

COST:
    ~$0.002 per outcomes PDF with Haiku — trivial.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, unquote
from urllib.request import Request, urlopen

from script_report.config import DATA_DIR, HAIKU_MODEL
from script_report.utils.helpers import MONTH_MAP, load_dotenv_safely

load_dotenv_safely()

OUTCOMES_DIR = DATA_DIR / "outcomes"
SOURCE_LIST = DATA_DIR / "outcomes_sources.json"
OUTPUT_JSON = DATA_DIR / "pbac_outcomes.json"

USER_AGENT = "script.report outcomes-fetcher (+https://script.report)"


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
        return []
    try:
        data = json.loads(SOURCE_LIST.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"  outcomes_sources.json is not valid JSON: {e}")
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
    wrapper and find the outcomes PDF link."""
    if source_url.lower().endswith(".pdf"):
        return source_url
    try:
        html = _http_get(source_url, accept="text/html").decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"    [warn] could not fetch wrapper {source_url} ({e})")
        return None
    candidates = PDF_HREF_RE.findall(html)
    if not candidates:
        print(f"    [warn] no PDF link found in {source_url}")
        return None
    # Prefer the main outcomes PDF over the DUSC report (DUSC has its own filename).
    preferred = [
        h for h in candidates
        if "outcomes" in h.lower() and "dusc" not in h.lower()
    ]
    if not preferred:
        preferred = [h for h in candidates if "outcomes" in h.lower()]
    href = (preferred or candidates)[0]
    return urljoin(source_url, href)


def download_pdf(pdf_url: str) -> Path | None:
    """Save the PDF to data/outcomes/<filename>. Idempotent — re-downloads
    each call so a republished v2/v3 PDF gets picked up automatically."""
    OUTCOMES_DIR.mkdir(parents=True, exist_ok=True)
    filename = unquote(pdf_url.rsplit("/", 1)[-1])
    target = OUTCOMES_DIR / filename
    try:
        body = _http_get(pdf_url, accept="application/pdf")
    except Exception as e:
        print(f"    [warn] download failed: {pdf_url} ({e})")
        return None
    target.write_bytes(body)
    return target


# ── PDF text extraction ──────────────────────────────────────────────────────

def extract_pdf_text(pdf_path: Path) -> str:
    """Pull text from every page. Outcomes summaries are short (3–10 pages)."""
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
    r"(?P<year>\d{4})\s+PBAC(?:\s+Intracycle)?\s+Meeting",
    re.IGNORECASE,
)

_FILENAME_DATE_RE = re.compile(
    r"(?:^|[-_/])(\d{2})[-_](\d{4})(?:[-_]|\.|$)"
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
        mon_int = MONTH_MAP.get(month_name[:3].lower())
        if mon_int:
            iso_date = f"{year:04d}-{mon_int:02d}-01"

    # Fallback: parse MM-YYYY out of the filename (e.g. pbac-web-outcomes-03-2026-v3.pdf).
    if not iso_date:
        fm = _FILENAME_DATE_RE.search(filename)
        if fm:
            try:
                mo = int(fm.group(1))
                yr = int(fm.group(2))
                if 1 <= mo <= 12 and 2000 <= yr <= 2099:
                    iso_date = f"{yr:04d}-{mo:02d}-01"
                    if not label:
                        month_name = list(MONTH_MAP.keys())[mo - 1].title()
                        # MONTH_MAP keys are 3-letter abbreviations — recover full name
                        full = {
                            1: "January", 2: "February", 3: "March", 4: "April",
                            5: "May", 6: "June", 7: "July", 8: "August",
                            9: "September", 10: "October", 11: "November", 12: "December",
                        }[mo]
                        label = f"{full} {yr} PBAC{' Intracycle' if is_intracycle else ''} Meeting"
            except ValueError:
                pass

    return label, iso_date, kind


# ── Haiku extraction ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a precise data extractor for Australian PBAC meeting outcomes "
    "summaries (the short 'Recommendations made by the PBAC' PDFs that "
    "publish before the full PSDs). Extract every submission row. Return only "
    "valid JSON."
)

USER_PROMPT_TEMPLATE = """Extract every drug/submission item from this PBAC outcomes summary.

The PDF is a short summary listing each submission considered at one PBAC
meeting (or Intracycle meeting) and the outcome PBAC reached. Items are
usually grouped under outcome headings ("Recommended", "Not recommended",
"Deferred"). Some PDFs split each outcome heading further by submission type
("Major submission – New listing", "Change to existing listing", etc.). Some
items also include a brief note about the indication or what the change is.

Return ONLY a JSON object of this shape (no markdown fences, no extra text):

{{
  "items": [
    {{
      "drug": "primary generic drug name(s) — uppercase, the heading line of the row (e.g. 'TUCATINIB', 'ADALIMUMAB and INFLIXIMAB')",
      "trade_name": "trade/brand name if shown in parentheses or beside the generic (e.g. 'TUKYSA®', 'Galafold®'), else null",
      "sponsor": "sponsor / applicant company exactly as shown, else null",
      "indication": "concise indication / condition phrase (e.g. 'Crohn disease', 'Breast cancer', 'Hereditary angioedema prophylaxis'). Keep it short — under ~120 chars. Strip filler.",
      "submission_category": "one of: Major submission | Minor submission | Resubmission | Internal submission | Stakeholder meeting | Post-market review | Matter arising | Other matter | Other. Pick from explicit row markers; otherwise infer 'Major submission' for new listings, 'Resubmission' if the text indicates a prior consideration.",
      "submission_pathway": "one of: New listing | Change to existing listing | New NIP listing | Pricing matter | PBS review | Standard Re-entry | Early Re-entry | Early Resolution | Facilitated Resolution | Other matters | null. Use the section heading or row text.",
      "outcome": "exactly one of: Recommended | Not recommended | Deferred | Withdrawn | Noted. Take from the section heading that contains this row.",
      "outcome_detail": "fuller outcome phrase if printed (e.g. 'Recommended with restriction', 'Recommended for inclusion in the General Schedule'), else null. Trim to ~120 chars."
    }}
  ]
}}

Rules:
- Process every row across every outcome section.
- Skip the introduction page and any procedural sections (e.g. 'About the PBAC', appendices).
- Do not invent fields. Use null if unsure.
- Order items as they appear in the document.
- If a row covers multiple drugs (e.g. 'ADALIMUMAB and INFLIXIMAB'), keep them combined in one item — do not split.
- If the same drug appears multiple times (e.g. with different indications), output one item per row — do not merge.

OUTCOMES PDF TEXT:
{text}
"""


def call_haiku(client: anthropic.Anthropic, text: str) -> dict:
    if len(text.strip()) < 80:
        return {"items": [], "error": "no usable text"}

    # The outcomes PDFs run 60+ pages (~150 KB of text) because each item gets
    # a multi-paragraph rationale. Haiku 4.5 has a 200K context window, so we
    # pass the whole document rather than truncating like the agenda extractor
    # does (agendas are 3–8 pages).
    prompt = USER_PROMPT_TEMPLATE.format(text=text[:180_000])
    retries = 0
    while retries <= 4:
        try:
            msg = client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=16_384,
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
        return {"meetings": [], "last_updated": None}
    try:
        return json.loads(OUTPUT_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"  [warn] {OUTPUT_JSON.name} is corrupt — starting fresh")
        return {"meetings": [], "last_updated": None}


def write_output(payload: dict) -> None:
    payload["last_updated"] = date.today().isoformat()
    OUTPUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Extract PBAC outcomes summaries")
    ap.add_argument("--re-extract", action="store_true",
                    help="Re-extract every source URL even if cached.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process only the first N sources (for testing).")
    args = ap.parse_args()

    sources = load_source_list()
    if args.limit:
        sources = sources[: args.limit]
    if not sources:
        print("Nothing to do — no sources in outcomes_sources.json.")
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key.startswith("sk-"):
        print("ERROR: ANTHROPIC_API_KEY not set. Add it to .env.")
        sys.exit(1)
    client = anthropic.Anthropic(api_key=api_key)

    cached = load_cached()
    by_url = {m["source_url"]: m for m in cached.get("meetings", [])}

    print("=" * 62)
    print(f"PBAC Outcomes Extractor — {len(sources)} source URL(s)")
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
        # Tally outcomes for the status line
        tally: dict[str, int] = {}
        for it in items:
            o = (it.get("outcome") or "").strip() or "Unknown"
            tally[o] = tally.get(o, 0) + 1
        tally_s = ", ".join(f"{k} {v}" for k, v in sorted(tally.items(), key=lambda kv: -kv[1]))
        print(f"    extracted {len(items)} items ({tally_s})")

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

    # Preserve original source order; stale entries fall to the end.
    in_order = [by_url[u] for u in sources if u in by_url]
    in_order += [m for u, m in by_url.items() if u not in sources]
    write_output({"meetings": in_order})

    total_items = sum(len(m.get("items") or []) for m in in_order)
    print()
    print(f"  Total cached    : {len(in_order)} meeting(s), {total_items} item(s)")
    print(f"  Output          : {OUTPUT_JSON.name}")
    print()
    print("Next: python -m script_report build")


if __name__ == "__main__":
    main()
