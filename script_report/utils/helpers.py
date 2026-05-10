"""Cross-script helpers: date/month parsing, data-path resolution, .env loading.

These were duplicated across build_site_data.py, extract_psd_text.py,
download_missing_psds.py, parse_pbac_calendar.py, and match_pbac_nice.py
(now removed). The audit (Commit F) flagged the duplication; consolidating
here.
"""

from __future__ import annotations

from pathlib import Path

from script_report.config import DATA_DIR, REPO_ROOT


# ── Month name / number lookup tables ────────────────────────────────────────
# Full + abbreviated names → 1-12.
MONTH_MAP: dict[str, int] = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# Full name → 3-letter abbreviation
MONTH_ABBR: dict[str, str] = {
    "january": "jan", "february": "feb", "march": "mar", "april": "apr",
    "may":     "may", "june":     "jun", "july":  "jul", "august":  "aug",
    "september": "sep", "october": "oct", "november": "nov", "december": "dec",
}

# 3-letter abbreviation → 2-digit zero-padded number
MONTH_NUM: dict[str, str] = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def data_path(filename: str) -> Path:
    """Resolve a data-file path: prefer ``data/`` subfolder, fall back to repo root.

    Useful during the data/ migration where some files were moved and others
    weren't. Once the migration is fully settled, callers can switch to the
    direct ``DATA_DIR / filename`` form.
    """
    candidate = DATA_DIR / filename
    return candidate if candidate.exists() else REPO_ROOT / filename


def load_dotenv_safely() -> None:
    """Load .env from the repo root if python-dotenv is available.

    Optional dependency — if dotenv isn't installed the caller's environment
    is used directly (which is what Vercel's build environment does anyway).
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass
