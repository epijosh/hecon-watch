"""
inspect_pbs_xls.py
━━━━━━━━━━━━━━━━━━
Run this once on the sample XLS file you downloaded from the
Medicare Statistics website to reveal its exact structure.

USAGE:
    python inspect_pbs_xls.py PBS_Data_2093507847.3.xls

    (drag the .xls file into the same folder as this script and run without an argument,
     or pass the full path)

REQUIREMENTS:
    pip install pandas xlrd openpyxl
"""

from __future__ import annotations
import sys
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    print("Run:  pip install pandas xlrd")
    sys.exit(1)


def inspect(path: Path):
    print(f"\n{'='*60}")
    print(f"File: {path.name}  ({path.stat().st_size:,} bytes)")
    print(f"{'='*60}\n")

    # Try xlrd first (old .xls format), then openpyxl (.xlsx)
    engine = "xlrd" if path.suffix.lower() == ".xls" else "openpyxl"
    try:
        sheets = pd.read_excel(path, sheet_name=None, header=None, engine=engine)
    except Exception as e:
        print(f"Error reading file: {e}")
        print(f"Try:  pip install xlrd")
        return

    for sheet_name, df in sheets.items():
        print(f"┌─ Sheet: '{sheet_name}'  ({df.shape[0]} rows × {df.shape[1]} cols)")
        print(f"│")

        # Show every row with row number so we can identify header position
        for i, row in df.iterrows():
            vals = [str(v) if pd.notna(v) else "" for v in row]
            nonempty = [v for v in vals if v and v != "nan"]
            if not nonempty:
                print(f"│  [{i:>3}]  <empty row>")
            else:
                print(f"│  [{i:>3}]  {' | '.join(vals[:10])}")
            if i >= 40:  # Show first 40 rows
                print(f"│  ... ({df.shape[0] - 40} more rows not shown)")
                break

        print(f"└{'─'*58}\n")


def main():
    # Find file: argument or auto-detect in current folder
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        # Look for PBS_Data*.xls in this script's folder
        here = Path(__file__).parent
        candidates = list(here.glob("PBS_Data*.xls")) + list(here.glob("PBS_Data*.xlsx"))
        if not candidates:
            print("No PBS_Data*.xls file found in this folder.")
            print("Usage: python inspect_pbs_xls.py <path-to-file.xls>")
            sys.exit(1)
        path = candidates[0]
        print(f"Auto-detected: {path.name}")

    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)

    inspect(path)
    print("\nCopy the output above and share it — this tells us exactly how to parse the download.")


if __name__ == "__main__":
    main()
