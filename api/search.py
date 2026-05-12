"""
api/search.py
━━━━━━━━━━━━━
Vercel Python serverless function for Smart Search over the PSD corpus.

REQUEST
  GET  /api/search?q=<query>&limit=<n>&filter_outcome=<recommended|not|deferred>

RESPONSE
  {
    "query":     "...",
    "model":     "voyage-3",
    "count":     <int>,
    "intent":    "factoid|drug_lookup|filter|precedent|legacy",
    "results":   [{drug, score, year, indication, outcome, profile}, ...],
    "synthesis": {"answer": "...", "citations": [...], "generated_from_count": N}  // or null
  }

ENV
  VOYAGE_API_KEY         — required, vector embedder
  ANTHROPIC_API_KEY      — required for v2 features (parser + synthesis)
  SMART_SEARCH_V2        — "0" / "false" disables parser + synthesis and serves
                            the legacy semantic-only flow (kill switch).
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import time
from pathlib import Path

ROOT     = Path(__file__).resolve().parents[1]
EMB_BIN  = ROOT / "data" / "psd_embeddings.bin"
EMB_META = ROOT / "data" / "psd_embeddings_meta.json"

_VECTORS = None
_META    = None
_VOYAGE  = None
_NAME_INDEX: dict[str, int] | None = None
_QUERY_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL_S = 300


def _v2_enabled() -> bool:
    v = (os.environ.get("SMART_SEARCH_V2") or "").strip().lower()
    return v not in ("0", "false", "no", "off")


def _ensure_loaded():
    global _VECTORS, _META, _VOYAGE, _NAME_INDEX
    if _VECTORS is None or _META is None:
        import numpy as np
        if not EMB_META.exists() or not EMB_BIN.exists():
            raise RuntimeError("Embedding files missing on the server.")
        _META = json.loads(EMB_META.read_text(encoding="utf-8"))
        n, d = int(_META["count"]), int(_META["dim"])
        raw = np.fromfile(EMB_BIN, dtype=np.float32).reshape(n, d)
        norms = np.linalg.norm(raw, axis=1, keepdims=True)
        norms[norms == 0] = 1
        _VECTORS = (raw / norms).astype(np.float32)
        _NAME_INDEX = {(d.get("name") or "").lower(): i for i, d in enumerate(_META["drugs"])}

    if _VOYAGE is None:
        import voyageai
        key = (os.environ.get("VOYAGE_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("VOYAGE_API_KEY not set")
        _VOYAGE = voyageai.Client(api_key=key)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalise_outcome(s: str) -> str:
    s = (s or "").lower()
    if s.startswith("recommended") and not s.startswith("not"):
        return "recommended"
    if s.startswith("not"):
        return "not"
    if s == "deferred":
        return "deferred"
    return s


def _embed_query(text: str):
    import numpy as np
    r = _VOYAGE.embed(texts=[text], model=_META["model"], input_type="query")
    qv = np.asarray(r.embeddings[0], dtype=np.float32)
    qn = np.linalg.norm(qv) or 1.0
    return qv / qn


def _apply_filters(indices: list[int], filters: dict, drugs: list[dict]) -> list[int]:
    """Filter a list of meta indices by structured criteria from the parser.
    Filters are skipped silently when the underlying meta field is absent
    (so older meta with fewer fields still degrades to a no-op)."""
    if not filters:
        return indices

    sponsor      = (filters.get("sponsor") or "").lower().strip() or None
    outcome      = _normalise_outcome(filters.get("outcome") or "") or None
    icer_band    = filters.get("icer_band")
    year_min     = filters.get("year_min")
    year_max     = filters.get("year_max")
    trial_design = (filters.get("trial_design") or "").lower().strip() or None
    therapy_area = (filters.get("therapy_area") or "").lower().strip() or None
    cost_basis   = (filters.get("cost_basis") or "").lower().strip() or None
    resubmission = filters.get("resubmission")

    out: list[int] = []
    for i in indices:
        m = drugs[i]
        if sponsor:
            if sponsor not in (m.get("sponsor") or "").lower():
                continue
        if outcome:
            if _normalise_outcome(m.get("outcome") or "") != outcome:
                continue
        if year_min is not None:
            try:
                if int(m.get("year") or 0) < int(year_min):
                    continue
            except (TypeError, ValueError):
                pass
        if year_max is not None:
            try:
                if int(m.get("year") or 0) > int(year_max):
                    continue
            except (TypeError, ValueError):
                pass
        if trial_design:
            if (m.get("trial_design") or "").lower() != trial_design:
                continue
        if therapy_area:
            if therapy_area not in (m.get("therapy_area") or "").lower():
                continue
        if cost_basis:
            em = (m.get("economic_model") or "").lower()
            v = m.get("icer_value")
            if cost_basis == "cost_min" and "cost-min" not in em and "cost min" not in em:
                continue
            if cost_basis == "icer" and not (isinstance(v, (int, float)) and v > 0):
                continue
            if cost_basis == "dominant" and "dominant" not in em:
                continue
        if icer_band:
            v = m.get("icer_value")
            if not isinstance(v, (int, float)) or v <= 0:
                continue
            if icer_band == "high" and v < 75000:
                continue
            if icer_band == "low" and v >= 75000:
                continue
        out.append(i)
    return out


def _serialise(indices: list[int], scores: list[float], drugs: list[dict]) -> list[dict]:
    return [
        {
            "drug":       drugs[i].get("name"),
            "score":      round(float(s), 4),
            "year":       drugs[i].get("year"),
            "indication": drugs[i].get("indication"),
            "outcome":    drugs[i].get("outcome"),
            "profile":    (drugs[i].get("profile") or "")[:600],
        }
        for s, i in zip(scores, indices)
    ]


def _rank_all(qv) -> tuple:
    import numpy as np
    sims = _VECTORS @ qv
    order = np.argsort(-sims)
    return sims, order


# ── Legacy flow (kill-switch ON / parser disabled) ────────────────────────────

def _legacy_search(q: str, limit: int, filter_outcome: str | None) -> dict:
    _ensure_loaded()
    qv = _embed_query(q)
    sims, order = _rank_all(qv)
    drugs = _META["drugs"]
    f_out = _normalise_outcome(filter_outcome or "") or None
    cap = max(1, min(int(limit), 50))

    picked_idx: list[int] = []
    picked_score: list[float] = []
    for idx in order.tolist():
        if f_out and _normalise_outcome(drugs[idx].get("outcome") or "") != f_out:
            continue
        picked_idx.append(idx)
        picked_score.append(float(sims[idx]))
        if len(picked_idx) >= cap:
            break

    return {
        "query":     q,
        "model":     _META["model"],
        "intent":    "legacy",
        "count":     len(picked_idx),
        "results":   _serialise(picked_idx, picked_score, drugs),
        "synthesis": None,
    }


# ── V2 flow ───────────────────────────────────────────────────────────────────

def _v2_search(q: str, limit: int, filter_outcome: str | None) -> dict:
    """Smart-search v2: parser-routed retrieval with grounded synthesis.
    Falls back to legacy search on any internal error."""
    _ensure_loaded()
    drugs = _META["drugs"]
    cap = max(1, min(int(limit), 50))

    # Run parser + initial embed in parallel. Embed uses the original query;
    # if the parser produces a cleaner semantic_query we re-embed (rare-ish).
    parsed = None
    parse_err = None
    qv = None
    embed_err = None

    def _do_parse():
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _query_parser import parse_query  # noqa: E402
        return parse_query(q)

    def _do_embed():
        return _embed_query(q)

    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_parse = ex.submit(_do_parse)
        fut_embed = ex.submit(_do_embed)
        try:
            parsed = fut_parse.result(timeout=8)
        except Exception as e:
            parse_err = e
        try:
            qv = fut_embed.result(timeout=8)
        except Exception as e:
            embed_err = e

    if embed_err is not None:
        raise embed_err  # no point continuing without an embedding

    # Parser failure → fall through to legacy flow on the same embed
    if parsed is None or parse_err is not None:
        sims, order = _rank_all(qv)
        f_out = _normalise_outcome(filter_outcome or "") or None
        picked_idx: list[int] = []
        picked_score: list[float] = []
        for idx in order.tolist():
            if f_out and _normalise_outcome(drugs[idx].get("outcome") or "") != f_out:
                continue
            picked_idx.append(idx)
            picked_score.append(float(sims[idx]))
            if len(picked_idx) >= cap:
                break
        return {
            "query":     q,
            "model":     _META["model"],
            "intent":    "legacy",
            "count":     len(picked_idx),
            "results":   _serialise(picked_idx, picked_score, drugs),
            "synthesis": None,
            "parse_error": (str(parse_err)[:120] if parse_err else None),
        }

    intent = (parsed.get("intent") or "precedent").lower()
    filters = parsed.get("filters") or {}
    if filter_outcome:
        filters = {**filters, "outcome": filter_outcome}

    sims, order = _rank_all(qv)

    # ── Drug-mention boost: if the parser identified a drug and it's in our
    # corpus, surface that drug's record(s) first.
    boosted: list[int] = []
    boosted_set: set[int] = set()
    dm = (parsed.get("drug_mention") or "").strip().lower()
    if dm and intent in ("factoid", "drug_lookup"):
        idx = (_NAME_INDEX or {}).get(dm)
        if idx is not None:
            boosted.append(idx)
            boosted_set.add(idx)
        else:
            # Substring fallback for multi-drug names ("abiraterone acetate" vs
            # parser saying "abiraterone")
            for nm, i in (_NAME_INDEX or {}).items():
                if dm in nm or nm in dm:
                    boosted.append(i)
                    boosted_set.add(i)
                    break

    # ── Build candidate list per intent
    if intent == "filter":
        # Filter first, then rank within
        all_idx = list(range(len(drugs)))
        filtered = _apply_filters(all_idx, filters, drugs)
        # Score-sort filtered set
        filtered.sort(key=lambda i: -float(sims[i]))
        picked_idx = filtered[:cap]
    elif intent in ("factoid", "drug_lookup"):
        rest = [i for i in order.tolist() if i not in boosted_set]
        rest = _apply_filters(rest, filters, drugs)
        picked_idx = boosted + rest[: max(0, cap - len(boosted))]
    else:  # precedent
        filtered_order = _apply_filters(order.tolist(), filters, drugs)
        picked_idx = filtered_order[:cap]

    picked_score = [float(sims[i]) for i in picked_idx]
    results = _serialise(picked_idx, picked_score, drugs)

    # ── Synthesis (skip for precedent and when no results)
    synthesis = None
    if intent != "precedent" and results:
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from _synthesis import synthesise  # noqa: E402
            # Pass the full meta records (richer than the serialised cards)
            full_records = [drugs[i] for i in picked_idx[:10]]
            synthesis = synthesise(q, full_records, intent)
        except Exception as e:
            print(f"synthesis failed: {type(e).__name__}: {str(e)[:200]}")
            synthesis = None

    return {
        "query":     q,
        "model":     _META["model"],
        "intent":    intent,
        "count":     len(results),
        "results":   results,
        "synthesis": synthesis,
        "drug_mention": parsed.get("drug_mention"),
        "brand_hits": parsed.get("_brand_hits", []),
    }


# ── Vercel handler ────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def _send(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=120")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            qs = parse_qs(urlparse(self.path).query)
        except Exception:
            return self._send(400, {"error": "bad query string"})

        q = (qs.get("q", [""])[0] or "").strip()
        if len(q) < 3:
            return self._send(400, {"error": "Provide a query of at least 3 characters via ?q=..."})

        try:
            limit = int(qs.get("limit", ["20"])[0])
        except ValueError:
            limit = 20
        f_out = (qs.get("filter_outcome", [""])[0] or None)
        v2_override = (qs.get("v2", [""])[0] or "").strip()  # debugging escape hatch

        # In-memory cache (per warm function instance)
        ck = f"{q}|{f_out or ''}|{limit}|{int(_v2_enabled() if not v2_override else v2_override == '1')}"
        hit = _QUERY_CACHE.get(ck)
        if hit and (time.time() - hit[0]) < _CACHE_TTL_S:
            return self._send(200, hit[1])

        try:
            use_v2 = _v2_enabled() if not v2_override else (v2_override == "1")
            if use_v2:
                try:
                    payload = _v2_search(q, limit, f_out)
                except Exception as e:
                    # Hard fallback: v2 path threw a non-recoverable error
                    print(f"v2 search failed; falling back: {type(e).__name__}: {str(e)[:200]}")
                    payload = _legacy_search(q, limit, f_out)
                    payload["v2_error"] = f"{type(e).__name__}: {str(e)[:120]}"
            else:
                payload = _legacy_search(q, limit, f_out)
            _QUERY_CACHE[ck] = (time.time(), payload)
            return self._send(200, payload)
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {str(e)[:200]}"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
