"""
api/_synthesis.py
━━━━━━━━━━━━━━━━━
Generate a one-paragraph grounded answer to a Smart Search query, using
the structured records of the top-K retrieved drugs as the only allowed
source of facts.

Grounding contract enforced in the prompt:
  1. Every factual claim must be backed by a record we passed in.
  2. Each claim must be cited as (drug, year).
  3. If the records don't answer the question, return null. No filler.

The caller is expected to hide the synthesis block when answer is null.
"""

from __future__ import annotations

import os

HAIKU_MODEL = "claude-haiku-4-5-20251001"

_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        from anthropic import Anthropic
        key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        _CLIENT = Anthropic(api_key=key)
    return _CLIENT


_TOOL_SCHEMA = {
    "name": "answer",
    "description": (
        "Either return a grounded one-paragraph answer citing the provided "
        "records, or return null when the records can't answer the question."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "answer": {
                "type": ["string", "null"],
                "description": (
                    "1–3 sentences. Each factual claim must reference a "
                    "(drug, year) pair from the provided records. If the "
                    "records do not contain the information requested, "
                    "return null. Never invent values. Never write filler "
                    "like 'here are some results that may interest you'."
                ),
            },
            "citations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "drug": {"type": "string"},
                        "year": {"type": "string"},
                    },
                    "required": ["drug", "year"],
                },
                "description": "List of (drug, year) records the answer drew from. Empty list if answer is null.",
            },
        },
        "required": ["answer", "citations"],
    },
}


_SYSTEM_PROMPT = """You answer questions about Australia's PBAC (Pharmaceutical Benefits Advisory Committee) drug submissions, citing only the structured records the user provides.

Hard rules:
1. Only state facts that appear in the records.
2. Cite each factual claim with (drug, year) inline (e.g. "Mirvetuximab was recommended in 2024…").
3. If the records don't contain the information requested, return answer=null. Do NOT produce filler such as "Here are some results that may interest you" or "The following submissions are related to…".
4. Be concise: 1–3 sentences. No headings, no bullets.
5. Money: when a record's icer_value is null, do not invent a number — say "ICER value redacted" or describe what the records do show (cost-minimisation, etc.).
6. Audience: pharma analysts and health economists. Plain language; no fluff."""


def _trim_record(r: dict) -> dict:
    """Strip the long ``profile`` blob to keep the input prompt tight, and
    coerce numeric ICER to a readable form."""
    icer = r.get("icer_value")
    icer_str = None
    if isinstance(icer, (int, float)) and icer > 0:
        icer_str = f"${int(icer)//1000}k AUD/QALY"
    return {
        "drug": r.get("name") or r.get("drug"),
        "year": r.get("year"),
        "indication": r.get("indication"),
        "outcome": r.get("outcome"),
        "sponsor": r.get("sponsor"),
        "comparator": r.get("comparator"),
        "icer": icer_str or r.get("icer_note") or None,
        "trial_design": r.get("trial_design"),
        "economic_model": r.get("economic_model"),
        "therapy_area": r.get("therapy_area"),
        "listing_type": r.get("listing_type"),
        "rejection_reasons": (r.get("rejection_reasons") or "")[:160] or None,
    }


def synthesise(query: str, records: list[dict], parsed_intent: str) -> dict | None:
    """Return ``{"answer": str|None, "citations": [...]}``. Returns None on
    any error so the caller can drop the synthesis block silently."""
    if not records:
        return None

    trimmed = [_trim_record(r) for r in records[:10]]

    import json as _json
    user_msg = (
        f"Query intent: {parsed_intent}\n\n"
        f"User query: {query}\n\n"
        f"Top retrieved PBAC records ({len(trimmed)}):\n"
        f"{_json.dumps(trimmed, ensure_ascii=False, indent=2)}"
    )

    msg = _client().messages.create(
        model=HAIKU_MODEL,
        max_tokens=300,
        system=_SYSTEM_PROMPT,
        tools=[_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "answer"},
        messages=[{"role": "user", "content": user_msg}],
    )

    for block in msg.content:
        if getattr(block, "type", None) == "tool_use":
            data = dict(block.input or {})
            ans = data.get("answer")
            if not ans or not str(ans).strip():
                return None
            return {
                "answer": str(ans).strip(),
                "citations": data.get("citations") or [],
                "generated_from_count": len(trimmed),
            }
    return None
