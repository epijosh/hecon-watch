"""
script_report.builders.brand_map_builder
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Build ``data/brand_to_generic.json`` from the union of
``data/pbs_schedule_atc.csv`` (wide coverage) and ``data/psd_extracted.csv``
(PSDs we have).

The serverless search function loads this map on cold start and rewrites
brand-only queries (``kesimpta``) to their generic (``ofatumumab``) before
embedding. Without it, brand queries miss because Voyage embeddings were
trained on text where the generic dominates.

Keys are normalised (lowercase, trademark/registered symbols stripped,
whitespace collapsed). Values are the normalised generic name.

Usage:
    python -m script_report brandmap
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from script_report.config import DATA_DIR


SCHEDULE_CSV = DATA_DIR / "pbs_schedule_atc.csv"
EXTRACTED_CSV = DATA_DIR / "psd_extracted.csv"
OUTPUT_JSON = DATA_DIR / "brand_to_generic.json"


_SUFFIX_RE = re.compile(r"[®™©℠]")
_BRACKETS_RE = re.compile(r"\s*\([^)]*\)\s*")
_DOSAGE_RE = re.compile(r"\b\d+(\.\d+)?\s*(mg|mcg|µg|g|ml|iu|units?|%)\b", re.I)


def _norm(s: str) -> str:
    s = (s or "").strip()
    s = _SUFFIX_RE.sub("", s)
    s = _BRACKETS_RE.sub(" ", s)
    s = _DOSAGE_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _split_brand_field(raw: str) -> list[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    # Brand fields sometimes hold "Verzenio, Verzenios" or "BrandA/BrandB"
    parts = re.split(r"[,/;|]", raw)
    return [p for p in (_norm(x) for x in parts) if p]


def build_map() -> dict[str, str]:
    out: dict[str, str] = {}

    if SCHEDULE_CSV.exists():
        with open(SCHEDULE_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                generic = _norm(row.get("drug_name") or "")
                if not generic:
                    continue
                for brand in _split_brand_field(row.get("brand_name") or ""):
                    if brand and brand != generic:
                        out.setdefault(brand, generic)

    if EXTRACTED_CSV.exists():
        with open(EXTRACTED_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if (row.get("extraction_ok") or "").lower() != "yes":
                    continue
                generic = _norm(row.get("drug") or "")
                if not generic:
                    continue
                for brand in _split_brand_field(row.get("brand_name") or ""):
                    if brand and brand != generic:
                        # PSD-extracted brands win: they reflect the listing
                        # most users actually saw.
                        out[brand] = generic

    return out


def main() -> None:
    print("=" * 58)
    print("  Brand -> generic map builder")
    print("=" * 58)
    print(f"  Source: {SCHEDULE_CSV.name} + {EXTRACTED_CSV.name}")

    mapping = build_map()
    OUTPUT_JSON.write_text(
        json.dumps(mapping, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"  Wrote {len(mapping):,} brand->generic entries -> {OUTPUT_JSON.name}")


if __name__ == "__main__":
    main()
