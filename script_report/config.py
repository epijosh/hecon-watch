"""Shared configuration: paths, model identifiers, batch sizes.

Keeping these in plain Python (vs YAML) means autocomplete works, there's no
parser dependency, and a typo at import time is a SyntaxError or NameError
rather than a silent KeyError at run time.
"""

from __future__ import annotations

from pathlib import Path


# ── Filesystem layout ────────────────────────────────────────────────────────
# REPO_ROOT is the project root: the parent of the script_report/ package.
REPO_ROOT: Path = Path(__file__).resolve().parent.parent
DATA_DIR:  Path = REPO_ROOT / "data"
PSDS_DIR:  Path = DATA_DIR / "psds"
SITE_DATA_JS: Path = REPO_ROOT / "site_data.js"

# Ensure DATA_DIR exists for downstream callers that write into it.
DATA_DIR.mkdir(exist_ok=True)


# ── Model identifiers ────────────────────────────────────────────────────────
HAIKU_MODEL  = "claude-haiku-4-5-20251001"     # extract_psd_text.py
VOYAGE_MODEL = "voyage-3"                       # embed_psds.py default


# ── Batch sizes / limits ─────────────────────────────────────────────────────
VOYAGE_BATCH_SIZE = 64        # Voyage handles up to 128 inputs per call
NEAREST_TOP_K     = 20        # how many neighbours we precompute per drug
SIMILARITY_TIE_EPSILON = 0.01  # cosine-tie threshold for ATC tiebreaker


# ── Voyage pricing (rough, USD per 1M tokens — used for cost estimates) ──────
VOYAGE_PRICE_PER_1M = {
    "voyage-3":         0.06,
    "voyage-3-lite":    0.02,
    "voyage-3-large":   0.18,
    "voyage-large-2":   0.12,
}
