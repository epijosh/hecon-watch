"""
parse_atc_data.py
━━━━━━━━━━━━━━━━━
Parses the two PBS ATC-level XLS files (which are actually HTML tables)
downloaded from medicarestatistics.humanservices.gov.au.

Produces:
  atc_services.csv   — PBS prescriptions by ATC class, year, state (1992–2026)
  atc_benefit.csv    — Government benefit ($) by ATC class, year, state

The site returns HTML with a .xls extension + UTF-8 BOM. This script handles it.

FILE NAMING:
  PBS_Data_*.3.xls  → Services (prescription count)      RPT_FMT=3
  PBS_Data_*.4.xls  → $Benefit (government outlay)       RPT_FMT=4

WHO ATC Level 1 classes in the data:
  A  Alimentary Tract and Metabolism
  B  Blood and Blood Forming Organs
  C  Cardiovascular System
  D  Dermatologicals
  G  Genito-Urinary System and Sex Hormones
  H  Systemic Hormonal Preparations (excl. sex hormones)
  J  Antiinfectives for Systemic Use
  L  Antineoplastic and Immunomodulating Agents
  M  Musculo-Skeletal System
  N  Nervous System
  P  Antiparasitic Products, Insecticides and Repellents
  R  Respiratory System
  S  Sensory Organs
  V  Various

REQUIREMENTS:
  pip install beautifulsoup4 lxml

USAGE:
  python parse_atc_data.py
"""

from __future__ import annotations

import re
import csv
import sys
from pathlib import Path

HERE = Path(__file__).parent
DATA = HERE / "data"
DATA.mkdir(exist_ok=True)

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Run:  pip install beautifulsoup4 lxml")
    sys.exit(1)

# ── Constants ─────────────────────────────────────────────────────────────────
STATES = ["nsw", "vic", "qld", "sa", "wa", "tas", "act", "nt"]

# ATC Level 1 classes — used to recognise ATC name rows
ATC_CLASSES = {
    "alimentary tract and metabolism": "A",
    "blood and blood forming organs": "B",
    "cardiovascular system": "C",
    "dermatologicals": "D",
    "genito-urinary system and sex hormones": "G",
    "genitourinary system and sex hormones": "G",
    "systemic hormonal preparations": "H",
    "antiinfectives for systemic use": "J",
    "antineoplastic and immunomodulating agents": "L",
    "musculo-skeletal system": "M",
    "nervous system": "N",
    "antiparasitic products": "P",
    "respiratory system": "R",
    "sensory organs": "S",
    "various": "V",
}


def is_atc_name(text: str) -> bool:
    """True if text looks like a WHO ATC Level 1 class name."""
    t = text.lower().strip()
    return any(t.startswith(k) for k in ATC_CLASSES)


def atc_code(name: str) -> str:
    t = name.lower().strip()
    for k, v in ATC_CLASSES.items():
        if t.startswith(k[:12]):
            return v
    return "?"


def is_year(text: str) -> bool:
    t = text.strip()
    return bool(re.match(r'^(YTD\s*)?\d{4}$', t, re.I))


def parse_year(text: str) -> str:
    """Returns '2024' or '2026_ytd'."""
    t = text.strip()
    m = re.search(r'\d{4}', t)
    if not m:
        return ""
    yr = m.group()
    if "ytd" in t.lower():
        return f"{yr}_ytd"
    return yr


def parse_num(text: str) -> "int | None":
    cleaned = re.sub(r'[\$,\s]', '', str(text))
    try:
        return int(float(cleaned))
    except (ValueError, TypeError):
        return None


def parse_file(path: Path, metric: str) -> list[dict]:
    """
    Parse one HTML-disguised XLS file.
    Returns list of dicts: atc, atc_code, year, nsw, vic, ..., nt, total, metric
    """
    content = path.read_bytes().lstrip(b"\xef\xbb\xbf").decode("utf-8", errors="replace")
    soup    = BeautifulSoup(content, "lxml")

    # Find the largest table
    tables = soup.find_all("table")
    if not tables:
        print(f"  ERROR: No tables found in {path.name}")
        return []

    best_table = max(tables, key=lambda t: len(t.find_all("tr")))
    all_rows   = best_table.find_all("tr")

    records: list[dict] = []
    current_atc: str = ""

    SKIP_VALUES = {
        "scheme= pbs (click to drilldown to normal/special arrangements)",
        "atc classification", "calendar year", "state",
        "services", "$benefit", "nsw", "vic", "qld", "sa", "wa",
        "tas", "act", "nt", "total",
    }

    for tr in all_rows:
        cells = [td.get_text(separator=" ", strip=True)
                 for td in tr.find_all(["td", "th"])]
        if not cells:
            continue

        col0 = cells[0].strip()
        col1 = cells[1].strip() if len(cells) > 1 else ""

        # ── Row type 1: ATC name + "Calendar Year" + 1992 data ───────────────
        # e.g. "Alimentary Tract and Metabolism | Calendar Year | 4,662,179 | ..."
        if is_atc_name(col0) and "calendar year" in col1.lower() and len(cells) >= 10:
            current_atc = col0
            rec = _make_record(current_atc, "1992", cells[2:], metric)
            if rec:
                records.append(rec)
            continue

        # ── Row type 2: ATC name + year (usually 1993, often missing data) ───
        # e.g. "Alimentary Tract and Metabolism | 1993" (2 cols only)
        if is_atc_name(col0) and is_year(col1):
            current_atc = col0
            # 1993 often has only 2 cols due to rowspan — extract data if present
            if len(cells) >= 10:
                rec = _make_record(current_atc, parse_year(col1), cells[2:], metric)
                if rec:
                    records.append(rec)
            # else: skip — 1993 data genuinely absent in source
            continue

        # ── Row type 3: Previous ATC appears again + "Calendar Year" ─────────
        # Happens at the start of some ATC groups due to rowspan rendering
        if is_atc_name(col0) and "calendar year" in col1.lower():
            current_atc = col0
            continue

        # ── Row type 4: Year only + data ─────────────────────────────────────
        # e.g. "1994 | 5,059,712 | ..." or "YTD 2026 | 3,012,385 | ..."
        if is_year(col0) and current_atc and len(cells) >= 9:
            rec = _make_record(current_atc, parse_year(col0), cells[1:], metric)
            if rec:
                records.append(rec)
            continue

        # ── Row type 5: Total row ─────────────────────────────────────────────
        if col0.lower() in ("total", "grand total", "sub total"):
            # Record the ATC class total
            if current_atc and len(cells) >= 9:
                rec = _make_record(current_atc, "TOTAL", cells[1:], metric)
                if rec:
                    records.append(rec)
            continue

        # Everything else: skip (header rows, blanks)

    print(f"  Parsed {len(records)} rows from {path.name}")
    return records


def _make_record(atc: str, year: str, data_cells: list[str], metric: str) -> "dict | None":
    """Build a record dict from the state data cells."""
    nums = [parse_num(c) for c in data_cells]
    # Need at least 8 state values + 1 total = 9
    if len(nums) < 8:
        return None
    rec: dict = {
        "atc":      atc,
        "atc_code": atc_code(atc),
        "year":     year,
        "metric":   metric,
    }
    for i, state in enumerate(STATES):
        rec[state] = nums[i] if i < len(nums) else None
    rec["total"] = nums[8] if len(nums) > 8 else None
    return rec


def save_csv(records: list[dict], path: Path):
    if not records:
        print(f"  No records to save for {path.name}")
        return
    fields = list(records[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(records)
    print(f"  Saved: {path.name}  ({len(records):,} rows)")


def main():
    print("=" * 60)
    print("PBS ATC Data Parser")
    print("=" * 60)

    # Auto-detect the two files
    xls_files = sorted(HERE.glob("PBS_Data*.xls")) + sorted(HERE.glob("PBS_Data*.xlsx"))
    if not xls_files:
        print("ERROR: No PBS_Data*.xls files found in this folder.")
        print("Copy both downloaded XLS files here first.")
        sys.exit(1)

    # Assign metric based on filename suffix (.3 = services, .4 = benefit)
    services_file = None
    benefit_file  = None
    for f in xls_files:
        name = f.name.lower()
        if name.endswith(".3.xls") or name.endswith(".3.xlsx"):
            services_file = f
        elif name.endswith(".4.xls") or name.endswith(".4.xlsx"):
            benefit_file = f

    if not services_file and not benefit_file:
        # Fallback: sort by filename and assign
        xls_files = sorted(xls_files)
        if len(xls_files) >= 2:
            services_file, benefit_file = xls_files[0], xls_files[1]
        elif xls_files:
            services_file = xls_files[0]

    all_records = []

    if services_file:
        print(f"\nParsing services file: {services_file.name}")
        recs = parse_file(services_file, "services")
        all_records.extend(recs)
        save_csv(recs, DATA / "atc_services.csv")

    if benefit_file:
        print(f"\nParsing benefit file: {benefit_file.name}")
        recs = parse_file(benefit_file, "benefit_aud")
        all_records.extend(recs)
        save_csv(recs, DATA / "atc_benefit.csv")

    # Summary stats
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    atc_names = sorted(set(r["atc"] for r in all_records if r["year"] not in ("TOTAL",)))
    years = sorted(set(r["year"] for r in all_records
                       if r["year"] not in ("TOTAL",) and not r["year"].endswith("_ytd")))

    print(f"  ATC classes found : {len(atc_names)}")
    for name in atc_names:
        code = atc_code(name)
        print(f"    [{code}] {name}")

    print(f"\n  Year range        : {years[0] if years else '?'} – {years[-1] if years else '?'}")
    print(f"  Total rows parsed : {len(all_records):,}")
    print()

    # Print total 2024 benefit by ATC class for a quick sanity check
    benefit_2024 = [r for r in all_records if r["metric"] == "benefit_aud" and r["year"] == "2024"]
    if benefit_2024:
        print("  2024 Government Benefit by ATC class (AUD):")
        for r in sorted(benefit_2024, key=lambda x: -(x["total"] or 0)):
            if r["year"] == "TOTAL":
                continue
            total_b = r["total"] or 0
            print(f"    [{r['atc_code']}] {r['atc'][:45]:<45}  ${total_b/1e9:.2f}B")

    print("\nNext step: run build_site_data.py to embed this into the website.")


if __name__ == "__main__":
    main()
