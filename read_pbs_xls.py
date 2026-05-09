"""
read_pbs_xls.py
━━━━━━━━━━━━━━━
Reads the PBS ATC usage XLS files and shows their exact structure.

NOTE: Despite the .xls extension, the Medicare Statistics site actually
returns HTML tables with a UTF-8 BOM. This script handles both formats.

USAGE:
    1. Make sure PBS_Data*.xls files are in this folder (PSD_database)
    2. Run:  python read_pbs_xls.py

REQUIREMENTS:
    pip install beautifulsoup4 lxml pandas
"""

import sys
import re
from pathlib import Path

HERE = Path(__file__).parent
OUT  = HERE / "xls_structure.txt"

lines = []

def log(s=""):
    lines.append(s)
    print(s)


def is_html(path: Path) -> bool:
    with open(path, "rb") as f:
        header = f.read(20)
    # UTF-8 BOM + <!DOC or just <!DOC or <html
    return (header.lstrip(b"\xef\xbb\xbf")[:5].lower() in
            (b"<!doc", b"<html", b"<?xml"))


def parse_html_file(path: Path) -> list[list[str]]:
    """Parse an HTML-disguised-as-XLS file into a list of rows."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log("  Run: pip install beautifulsoup4 lxml")
        return []

    content = path.read_bytes().lstrip(b"\xef\xbb\xbf").decode("utf-8", errors="replace")
    soup = BeautifulSoup(content, "html.parser")

    # Find the largest table
    tables = soup.find_all("table")
    if not tables:
        log("  No <table> found in HTML")
        return []

    best = max(tables, key=lambda t: len(t.find_all("tr")))
    rows = []
    for tr in best.find_all("tr"):
        cells = [td.get_text(separator=" ", strip=True) for td in tr.find_all(["th", "td"])]
        if cells:
            rows.append(cells)
    return rows


def parse_excel_file(path: Path) -> list[list[str]]:
    """Parse a genuine XLS/XLSX binary file."""
    try:
        import pandas as pd
    except ImportError:
        log("  Run: pip install pandas xlrd openpyxl")
        return []
    engine = "xlrd" if path.suffix.lower() == ".xls" else "openpyxl"
    try:
        df = pd.read_excel(path, header=None, engine=engine, dtype=str)
        return df.fillna("").values.tolist()
    except Exception as e:
        log(f"  Excel parse error: {e}")
        return []


# ── Find files ────────────────────────────────────────────────────────────────
candidates = sorted(HERE.glob("PBS_Data*.xls")) + sorted(HERE.glob("PBS_Data*.xlsx"))

if not candidates:
    log("No PBS_Data*.xls files found in this folder.")
    log("Copy both downloaded XLS files here, then re-run.")
    sys.exit(1)

log(f"Found {len(candidates)} file(s):")
for c in candidates:
    log(f"  {c.name}  ({c.stat().st_size:,} bytes)")
log()

# ── Parse each file ───────────────────────────────────────────────────────────
for path in candidates:
    log("=" * 70)
    log(f"FILE: {path.name}")
    log("=" * 70)

    if is_html(path):
        log("  (detected: HTML table disguised as .xls)")
        rows = parse_html_file(path)
    else:
        log("  (detected: genuine Excel binary)")
        rows = parse_excel_file(path)

    if not rows:
        log("  No data found.")
        continue

    log(f"  {len(rows)} rows × {max(len(r) for r in rows)} cols")
    log()
    for i, row in enumerate(rows[:55]):
        nonempty = [v for v in row if str(v).strip()]
        if not nonempty:
            log(f"    [{i:02d}] <empty>")
            continue
        row_str = " | ".join(str(v) for v in row)
        if len(row_str) > 150:
            row_str = row_str[:150] + "…"
        log(f"    [{i:02d}] {row_str}")
    if len(rows) > 55:
        log(f"    ... ({len(rows) - 55} more rows not shown)")
    log()

log("=" * 70)
log(f"Output written to: {OUT}")

OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"\n✓ Done — check xls_structure.txt in this folder.")
