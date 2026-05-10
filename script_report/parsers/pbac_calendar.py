"""
parse_pbac_calendar.py
━━━━━━━━━━━━━━━━━━━━━━
One-shot parser for PBS Cycle Timeframe PDFs (the official PBAC submission
calendar published at https://www.pbs.gov.au/info/industry/listing/elements/pbac-meetings).

What it does
  - Finds every PBS-Cycle-timeframe-*.pdf in the project root (or takes --pdf paths)
  - Extracts tables and detects PBAC meetings (March / July / November of any year)
  - Classifies each row label as a known milestone (submission deadline, pre-PBAC,
    meeting, outcomes, PSDs, listing) using fuzzy regex
  - Parses Australian-style dates ('Friday 14 March 2024', '14/03/2024', etc.)
  - Writes data/pbac_calendar.json — additive merge by default, so hand-keyed
    fields are never overwritten

Output schema (data/pbac_calendar.json):
  {
    "meetings": [
      {
        "code": "2026-07",
        "label": "July 2026 PBAC meeting",
        "meeting_date": "2026-07-08",
        "deadlines": [
          {"key": "submission_major", "name": "Major submission deadline", "date": "2026-03-04"},
          ...
        ]
      },
      ...
    ],
    "source": "...",
    "source_files": [...],
    "last_updated": "2026-05-10"
  }

Run:
  python parse_pbac_calendar.py
  python parse_pbac_calendar.py --pdf PBS-Cycle-timeframe-2026-2027.pdf
  python parse_pbac_calendar.py --print          # dump parsed JSON to stdout
  python parse_pbac_calendar.py --no-merge       # overwrite, don't merge

Then run build_site_data.py to fold it into site_data.js.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    print("Missing dependency. Run:\n  pip install pdfplumber --break-system-packages")
    sys.exit(1)

from script_report.config import REPO_ROOT, DATA_DIR
from script_report.utils.helpers import MONTH_MAP as MONTHS

HERE   = REPO_ROOT
DATA   = DATA_DIR
OUTPUT = DATA / "pbac_calendar.json"

# ── Constants ────────────────────────────────────────────────────────────────
MONTH_NAME = {3: "March", 7: "July", 11: "November"}
PBAC_MONTHS = {3, 7, 11}     # PBAC meets three times a year
CYCLE_TO_MONTH = {1: 3, 2: 7, 3: 11}   # 2024/1 = March, /2 = July, /3 = November

# Canonical milestone keys (in chronological order within a cycle)
MILESTONE_ORDER = [
    "notification",
    "submission_major",
    "submission_minor",
    "prepbac_response",
    "meeting",
    "outcomes",
    "psds",
    "listing",
]
MILESTONE_LABEL = {
    "notification":     "Notification of intent",
    "submission_major": "Major submission deadline",
    "submission_minor": "Minor / resubmission deadline",
    "prepbac_response": "Pre-PBAC response",
    "meeting":          "PBAC meeting",
    "outcomes":         "Outcomes published",
    "psds":             "PSDs published",
    "listing":          "Earliest possible listing",
}

# Fuzzy classifier: text → milestone key. Order matters; first match wins,
# so put the more specific patterns at the top.
#
# Designed against the PBS Cycle Timeframe PDF format. Intracycle/December
# Intracycle rows are intentionally excluded so they don't clobber the main
# meeting's dates (we keep the canonical 8-step cycle, not the secondary loops).
LABEL_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Main submission deadline: "Deadline for Category 1 to 4 and Committee Secretariat submissions"
    (re.compile(r"deadline\s+for\s+category\s+1\s+to\s+4|category\s+1\s+to\s+4.+(submission|secretariat)", re.I), "submission_major"),
    # Notice of intent for the main category — week -4
    (re.compile(r"notice\s+of\s+intent.+(category|cat\.?\s*1)|category.+1.+notice\s+of\s+intent", re.I), "notification"),
    # Early Re-entry Pathway resubmissions — the canonical "minor" deadline
    (re.compile(r"early\s+re-?entry\s+pathway\s+resub|early\s+re-?entry\s+resubmission", re.I), "submission_minor"),
    # Pre-PBAC Response — week 16
    (re.compile(r"applicants?\s+send\s+pre-?pbac\s+responses?|pre-?pbac\s+response", re.I), "prepbac_response"),
    # The MAIN PBAC meeting — exclude Intracycle/December variants
    (re.compile(r"^\s*pbac\s+meeting\b(?!.*intracycle)(?!.*december)", re.I), "meeting"),
    # Outcomes posted — the "Posting of outcomes on website following Applicants' comments" row.
    # Exclude Intracycle/December variants.
    (re.compile(r"posting\s+of\s+outcomes?\s+on\s+website(?!.*intracycle)(?!.*december)", re.I), "outcomes"),
    # PSDs published — the "positive & subsequent rejections" row (week 33). Exclude Intracycle/December.
    (re.compile(r"psds?\s+published\s+on\s+website.+positive(?!.*intracycle)", re.I), "psds"),
    # Listing — usually not on this PDF, but cover it if it appears
    (re.compile(r"earliest\s+(?:listing|pbs)\s+listing|listed\s+on\s+pbs|f\W*1?\s*supply", re.I), "listing"),
]

# ── Date parsing ─────────────────────────────────────────────────────────────
DATE_RE_FULL = re.compile(
    r"(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+)?"   # optional weekday
    r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})",
    re.I,
)
DATE_RE_SLASH = re.compile(r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})\b")


def parse_date(text: str) -> date | None:
    if not text:
        return None
    m = DATE_RE_FULL.search(text)
    if m:
        try:
            day = int(m.group(1))
            mon = MONTHS.get(m.group(2)[:3].lower())
            yr  = int(m.group(3))
            if mon and 1 <= day <= 31 and 2000 <= yr <= 2099:
                return date(yr, mon, day)
        except (ValueError, AttributeError):
            pass
    m = DATE_RE_SLASH.search(text)
    if m:
        try:
            day, mon, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 1 <= day <= 31 and 1 <= mon <= 12 and 2000 <= yr <= 2099:
                return date(yr, mon, day)
        except ValueError:
            pass
    return None


# ── Label / meeting classification ───────────────────────────────────────────
def classify_label(label: str) -> str | None:
    if not label:
        return None
    for pat, key in LABEL_PATTERNS:
        if pat.search(label):
            return key
    return None


MEETING_HEADER_RE  = re.compile(r"\b(March|July|November)\s+(\d{4})\b", re.I)
MEETING_CYCLE_RE   = re.compile(r"\b(\d{4})\s*/\s*([1-3])\b")   # e.g. 2024/1


def detect_meeting(text: str) -> tuple[int, int] | None:
    """Return (year, month) if `text` mentions a PBAC meeting in either:
       - "2024/1" / "2024/2" / "2024/3" cycle code form, or
       - "March 2024" / "July 2024" / "November 2024" form.
    """
    if not text:
        return None
    # 2024/1 → March 2024, 2024/2 → July, 2024/3 → November
    m = MEETING_CYCLE_RE.search(text)
    if m:
        yr  = int(m.group(1))
        cyc = int(m.group(2))
        mon = CYCLE_TO_MONTH.get(cyc)
        if mon and 2000 <= yr <= 2099:
            return (yr, mon)
    # Month-name form
    m = MEETING_HEADER_RE.search(text)
    if m:
        mon = MONTHS.get(m.group(1).lower())
        yr  = int(m.group(2))
        if mon in PBAC_MONTHS:
            return (yr, mon)
    return None


def code_for(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


# ── Table processing ─────────────────────────────────────────────────────────
def process_table(rows: list[list[str | None]], out: dict[str, dict[str, str]],
                   debug_labels: list[str] | None = None):
    """Extract milestone dates from a single pdfplumber table.

    Handles the PBS layout where each meeting header spans TWO sub-columns
    ("Wk" + "Date") — the meeting code only appears in one of them, so we
    inherit the code forward to its date sub-column."""
    if not rows or len(rows) < 2:
        return
    rows = [[(c or "").strip() for c in row] for row in rows]

    # ── Layout A — columns are meetings, rows are milestones ─────────────────
    # Scan first few header rows for any cell that names a PBAC meeting
    # (e.g. "2024/1\n13-15 March" or "March 2024").
    anchor: dict[int, str] = {}
    n_cols = max(len(r) for r in rows)
    for hdr_row in rows[:3]:
        for i, cell in enumerate(hdr_row):
            mt = detect_meeting(cell)
            if mt:
                anchor.setdefault(i, code_for(*mt))

    if anchor:
        # Inherit forward: a meeting label sitting in one sub-column propagates
        # to its sibling sub-column (Wk → Date), but never jumps to the next
        # meeting (cap inheritance distance at 1 column).
        col_to_meeting: dict[int, str] = {}
        last_code: str | None = None
        last_col: int = -10
        for col in range(n_cols):
            if col in anchor:
                last_code = anchor[col]
                last_col = col
                col_to_meeting[col] = last_code
            elif last_code and (col - last_col) <= 1:
                col_to_meeting[col] = last_code
                last_col = col

        for row in rows:
            label_cell = row[0] if row else ""
            if debug_labels is not None and label_cell:
                debug_labels.append(label_cell)
            key = classify_label(label_cell)
            if not key:
                continue
            for i, cell in enumerate(row):
                if i not in col_to_meeting:
                    continue
                d = parse_date(cell)
                if not d:
                    continue
                code = col_to_meeting[i]
                out.setdefault(code, {}).setdefault(key, d.isoformat())
        return

    # ── Layout B — rows are meetings, columns are milestones ────────────────
    header = rows[0]
    col_to_milestone: dict[int, str] = {}
    for i, cell in enumerate(header):
        key = classify_label(cell)
        if key:
            col_to_milestone[i] = key
    if not col_to_milestone:
        return
    for row in rows[1:]:
        mt: tuple[int, int] | None = None
        for cell in row[:3]:
            mt = detect_meeting(cell)
            if mt:
                break
        if not mt:
            continue
        code = code_for(*mt)
        for i, cell in enumerate(row):
            if i not in col_to_milestone:
                continue
            d = parse_date(cell)
            if not d:
                continue
            out.setdefault(code, {}).setdefault(col_to_milestone[i], d.isoformat())


# ── Text fallback (only triggers if a line names BOTH a meeting and a date) ──
def process_text_line(line: str, out: dict):
    mt = detect_meeting(line)
    if not mt:
        return
    key = classify_label(line)
    d   = parse_date(line)
    if key and d:
        out.setdefault(code_for(*mt), {}).setdefault(key, d.isoformat())


# ── PDF entry point ──────────────────────────────────────────────────────────
def extract_from_pdf(pdf_path: Path, verbose: bool = True,
                      debug_labels: list[str] | None = None) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    with pdfplumber.open(pdf_path) as pdf:
        for pi, page in enumerate(pdf.pages, 1):
            tables = page.extract_tables() or []
            for tbl in tables:
                before = sum(len(v) for v in out.values())
                process_table(tbl, out, debug_labels=debug_labels)
                added = sum(len(v) for v in out.values()) - before
                if verbose and added:
                    print(f"    p.{pi:>2}  table  +{added} dates")

            text = page.extract_text() or ""
            for line in text.split("\n"):
                process_text_line(line, out)
    return out


def find_default_pdfs() -> list[Path]:
    candidates = (
        list(HERE.glob("PBS-Cycle*.pdf"))  + list(HERE.glob("PBS-cycle*.pdf")) +
        list(DATA.glob("PBS-Cycle*.pdf")) + list(DATA.glob("PBS-cycle*.pdf"))
    )
    return sorted({p.resolve(): None for p in candidates})  # dedup


# ── JSON shaping ─────────────────────────────────────────────────────────────
def to_payload(extracted: dict, source_files: list[Path]) -> dict:
    meetings = []
    for code in sorted(extracted.keys()):
        ms = extracted[code]
        year, mo = code.split("-")
        label = f"{MONTH_NAME.get(int(mo), '?')} {year} PBAC meeting"
        deadlines = []
        for key in MILESTONE_ORDER:
            if key in ms:
                deadlines.append({"key": key, "name": MILESTONE_LABEL[key], "date": ms[key]})
        meetings.append({
            "code":          code,
            "label":         label,
            "meeting_date":  ms.get("meeting"),
            "deadlines":     deadlines,
        })
    return {
        "meetings":      meetings,
        "source":        "Parsed from PBS Cycle Timeframe PDF(s) by parse_pbac_calendar.py",
        "source_files":  [p.name for p in source_files],
        "last_updated":  date.today().isoformat(),
    }


# ── Merge with existing JSON (additive — never overwrite) ────────────────────
def merge_payloads(new: dict, existing_path: Path) -> dict:
    if not existing_path.exists():
        return new
    try:
        existing = json.loads(existing_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ⚠  Existing {existing_path.name} is unreadable ({e}); overwriting.")
        return new

    existing_by_code = {m["code"]: m for m in existing.get("meetings", [])}
    new_by_code      = {m["code"]: m for m in new.get("meetings", [])}

    merged = []
    for code in sorted(set(existing_by_code) | set(new_by_code)):
        if code in existing_by_code and code in new_by_code:
            ex_dl = {d["key"]: d for d in existing_by_code[code].get("deadlines", [])}
            nw_dl = {d["key"]: d for d in new_by_code[code].get("deadlines", [])}
            combined: dict = dict(ex_dl)
            for k, d in nw_dl.items():
                combined.setdefault(k, d)   # existing wins
            ordered = [combined[k] for k in MILESTONE_ORDER if k in combined]
            merged.append({
                **existing_by_code[code],
                **{k: v for k, v in new_by_code[code].items()
                   if k not in existing_by_code[code] or not existing_by_code[code].get(k)},
                "deadlines": ordered,
            })
        elif code in existing_by_code:
            merged.append(existing_by_code[code])
        else:
            merged.append(new_by_code[code])

    return {
        **new,
        "meetings":     merged,
        "last_updated": date.today().isoformat(),
    }


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Parse PBS Cycle Timeframe PDFs into pbac_calendar.json")
    ap.add_argument("--pdf",      nargs="*", help="Path(s) to cycle-timeframe PDFs (default: auto-find PBS-Cycle*.pdf)")
    ap.add_argument("--out",      default=str(OUTPUT), help="Output JSON path")
    ap.add_argument("--no-merge", action="store_true", help="Overwrite output instead of merging with existing")
    ap.add_argument("--print",    action="store_true", help="Print parsed JSON to stdout")
    ap.add_argument("--quiet",    action="store_true", help="Less verbose output")
    ap.add_argument("--debug",    action="store_true", help="Print every row label the parser saw (useful when no meetings are detected)")
    args = ap.parse_args()

    if args.pdf:
        paths = [Path(p).expanduser().resolve() for p in args.pdf]
    else:
        paths = find_default_pdfs()

    if not paths:
        print("No PBS-Cycle-*.pdf found in project root.")
        print("Pass --pdf path/to/file.pdf, or download the latest cycle PDF from:")
        print("  https://www.pbs.gov.au/info/industry/listing/elements/pbac-meetings")
        sys.exit(1)

    print("=" * 65)
    print("PBAC Calendar Parser")
    print("=" * 65)
    for p in paths:
        print(f"  Source: {p.name}")
    print()

    merged: dict[str, dict[str, str]] = {}
    debug_labels: list[str] = [] if args.debug else None
    for p in paths:
        if not p.exists():
            print(f"  ⚠ Missing: {p}")
            continue
        print(f"  Parsing {p.name}...")
        extracted = extract_from_pdf(p, verbose=not args.quiet, debug_labels=debug_labels)
        for code, milestones in extracted.items():
            merged.setdefault(code, {}).update({
                k: v for k, v in milestones.items() if k not in merged[code]
            })

    if args.debug and debug_labels is not None:
        print()
        print("─" * 65)
        print("Row labels the parser saw (with classification):")
        print("─" * 65)
        unique_labels = list(dict.fromkeys(debug_labels))
        for lbl in unique_labels:
            key = classify_label(lbl)
            tag = f"[{key:<18}]" if key else "[unclassified     ]"
            print(f"  {tag}  {lbl[:100]}")
        print()

    if not merged:
        print()
        print("⚠  No PBAC meetings detected. The PDF layout may differ from expected.")
        print("   Try:  python parse_pbac_calendar.py --debug   to see all row labels")
        print("   Or hand-key data/pbac_calendar.json directly.")
        sys.exit(1)

    payload = to_payload(merged, paths)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not args.no_merge:
        payload = merge_payloads(payload, out_path)

    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print()
    print(f"Wrote {out_path}")
    print(f"  Meetings   : {len(payload['meetings'])}")
    for m in payload["meetings"]:
        print(f"    {m['code']}  ·  {len(m['deadlines'])} milestones"
              f"  ·  meeting {m.get('meeting_date') or '?'}")

    if args.print:
        print()
        print(json.dumps(payload, indent=2))

    print()
    print("Next:")
    print("  1. Eyeball data/pbac_calendar.json and hand-edit any wrong/missing dates.")
    print("  2. Run:  python build_site_data.py   →   to expose calendar to the dashboard.")


if __name__ == "__main__":
    main()
