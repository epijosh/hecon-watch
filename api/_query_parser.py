"""
api/_query_parser.py
━━━━━━━━━━━━━━━━━━━━
Classify a Smart Search query into a structured intent + filters using
Claude Haiku. Lets api/search.py route to the right retrieval path and
extract structured filters from natural-language input.

Cold-start cost: ~5MB anthropic SDK + ~150ms client init.
Per-call cost:   ~400ms latency, ~$0.0008 (~500 in, ~150 out tokens).

The parser is best-effort. If Haiku errors / returns malformed output, the
caller is expected to fall back to plain semantic search.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAND_MAP_JSON = ROOT / "data" / "brand_to_generic.json"

HAIKU_MODEL = "claude-haiku-4-5-20251001"

_CLIENT = None
_BRAND_MAP: dict[str, str] | None = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        from anthropic import Anthropic
        key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        _CLIENT = Anthropic(api_key=key)
    return _CLIENT


def _brand_map() -> dict[str, str]:
    global _BRAND_MAP
    if _BRAND_MAP is None:
        if BRAND_MAP_JSON.exists():
            _BRAND_MAP = json.loads(BRAND_MAP_JSON.read_text(encoding="utf-8"))
        else:
            _BRAND_MAP = {}
    return _BRAND_MAP


def _strip_diacritics_lower(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def normalise_brands(query: str) -> tuple[str, list[tuple[str, str]]]:
    """Rewrite brand tokens to ``brand (generic)`` form so Voyage's
    embedding sees the generic name without losing the brand. Returns
    the rewritten query and a list of (brand, generic) tuples found.
    """
    bm = _brand_map()
    if not bm or not query:
        return query, []

    # Match brand strings up to 4 words long (PBS Schedule has "yonsa mpred",
    # "prevenar 20", etc.). Sort by length desc so longer brands win.
    found: list[tuple[str, str]] = []
    out = query
    lower = _strip_diacritics_lower(out)
    seen_brands: set[str] = set()
    # Iterate over brand keys sorted by length (longest first)
    for brand in sorted(bm.keys(), key=len, reverse=True):
        if len(brand) < 4:
            continue
        if brand in seen_brands:
            continue
        # Word-boundary match (case-insensitive) so "alec" doesn't match "alectinib"
        pattern = r"\b" + re.escape(brand) + r"\b"
        if re.search(pattern, lower):
            generic = bm[brand]
            if generic.lower() not in lower:
                out = re.sub(pattern, f"{brand} ({generic})", out, flags=re.IGNORECASE)
                lower = _strip_diacritics_lower(out)
            found.append((brand, generic))
            seen_brands.add(brand)
            if len(found) >= 3:
                break
    return out, found


_TOOL_SCHEMA = {
    "name": "classify_query",
    "description": (
        "Classify a search query against Australia's PBAC (Pharmaceutical "
        "Benefits Advisory Committee) Public Summary Document corpus. "
        "Decide what kind of question this is and extract any structured "
        "filters embedded in the query."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["factoid", "drug_lookup", "filter", "precedent"],
                "description": (
                    "factoid = question about a specific value for a specific drug "
                    "(e.g. 'what ICER was X recommended at'). "
                    "drug_lookup = user named a drug and wants its record (e.g. 'mirvetuximab'). "
                    "filter = structured list / aggregate ('Pfizer drugs', 'approved with high ICER'). "
                    "precedent = open-ended similarity search ('first-in-class oncology with immature OS')."
                ),
            },
            "drug_mention": {
                "type": ["string", "null"],
                "description": "Normalised generic drug name if a specific drug is named, else null.",
            },
            "requested_field": {
                "type": ["string", "null"],
                "enum": ["icer", "comparator", "outcome", "spend", "sponsor", "trial", "rejection", None],
                "description": "If factoid, which structured field the user wants. Else null.",
            },
            "filters": {
                "type": "object",
                "properties": {
                    "sponsor":      {"type": ["string", "null"]},
                    "outcome":      {"type": ["string", "null"], "enum": ["recommended", "not", "deferred", None]},
                    "icer_band":    {"type": ["string", "null"], "enum": ["low", "high", None]},
                    "year_min":     {"type": ["integer", "null"]},
                    "year_max":     {"type": ["integer", "null"]},
                    "trial_design": {"type": ["string", "null"], "enum": ["rct", "single_arm", "itc", "observational", None]},
                    "therapy_area": {"type": ["string", "null"]},
                    "cost_basis":   {"type": ["string", "null"], "enum": ["icer", "cost_min", "dominant", None]},
                    "resubmission": {"type": ["boolean", "null"]},
                },
                "required": [],
            },
            "semantic_query": {
                "type": "string",
                "description": (
                    "Cleaned query for vector retrieval. Strip filter words "
                    "('Pfizer', 'approved with high ICER') so embedding focuses "
                    "on conceptual content. Empty string allowed."
                ),
            },
        },
        "required": ["intent", "drug_mention", "requested_field", "filters", "semantic_query"],
    },
}


_SYSTEM_PROMPT = """You classify search queries against Australian PBAC (Pharmaceutical Benefits Advisory Committee) Public Summary Documents.

Each query falls into one of four intents:
- factoid:     specific named drug + specific structured field requested
- drug_lookup: user typed a drug name (or brand) with no other criteria
- filter:      structured list query — sponsor / outcome / ICER band / trial design / therapy area
- precedent:   open-ended conceptual similarity ("first-in-class oncology with immature OS")

Examples:
- "What ICER was mirvetuximab recommended at?" → factoid, drug=mirvetuximab, field=icer
- "mirvetuximab" → drug_lookup, drug=mirvetuximab
- "Which Pfizer medications" → filter, sponsor=Pfizer
- "approved with high icer" → filter, outcome=recommended, icer_band=high
- "failed but approved on resubmission" → filter, resubmission=true, outcome=recommended
- "did not have RCT" → filter, trial_design=single_arm
- "Surrogate endpoints" → precedent (open conceptual)
- "comparator is salvage therapy" → precedent
- "ovarian cancer approvals" → filter, therapy_area=oncology, outcome=recommended, semantic_query="ovarian cancer"

When in doubt between filter and precedent, prefer precedent. Never invent filter values that aren't supported by the query."""


def parse_query(query: str) -> dict:
    """Run query understanding. Returns the parsed dict; raises on any failure."""
    if not query or len(query.strip()) < 3:
        raise ValueError("query too short")

    rewritten, brand_hits = normalise_brands(query)

    msg = _client().messages.create(
        model=HAIKU_MODEL,
        max_tokens=400,
        system=_SYSTEM_PROMPT,
        tools=[_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "classify_query"},
        messages=[{"role": "user", "content": rewritten}],
    )

    for block in msg.content:
        if getattr(block, "type", None) == "tool_use":
            data = dict(block.input or {})
            data["_original_query"] = query
            data["_rewritten_query"] = rewritten
            data["_brand_hits"] = brand_hits
            # Force a usable semantic_query — if the model returned empty,
            # fall back to the rewritten query so vector retrieval still runs.
            if not (data.get("semantic_query") or "").strip():
                data["semantic_query"] = rewritten
            return data

    raise RuntimeError("query parser returned no tool_use block")
