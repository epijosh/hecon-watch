"""
api/search.py
━━━━━━━━━━━━━
Vercel Python serverless function for free-text semantic search over the PSD
embeddings produced by embed_psds.py.

REQUEST
  GET  /api/search?q=<query>&limit=<n>&filter_outcome=<recommended|not|deferred>
                  &filter_therapy=<area>

RESPONSE
  {
    "query":   "...",
    "model":   "voyage-3",
    "results": [
       {"drug": "...", "score": 0.81, "year": "2024", "indication": "...", "outcome": "..."},
       ...
    ]
  }

DEPLOY
  - Vercel auto-detects this as a Python function because it lives at api/search.py.
  - Add VOYAGE_API_KEY in Vercel project settings → Environment Variables.
  - Make sure data/psd_embeddings.bin and data/psd_embeddings_meta.json are
    committed to the repo (Vercel will deploy them alongside the function).
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import json
import os
from pathlib import Path

# Vercel mounts the repo at /var/task. Resolve data/ relative to repo root.
ROOT     = Path(__file__).resolve().parents[1]
EMB_BIN  = ROOT / "data" / "psd_embeddings.bin"
EMB_META = ROOT / "data" / "psd_embeddings_meta.json"

# ── Lazy globals (loaded once per function instance, reused on warm requests) ─
_VECTORS = None       # numpy float32 (n, dim), L2-normalised
_META    = None       # dict
_CLIENT  = None


def _ensure_loaded():
    global _VECTORS, _META, _CLIENT
    if _VECTORS is not None:
        return
    import numpy as np
    import voyageai

    if not EMB_META.exists() or not EMB_BIN.exists():
        raise RuntimeError("Embedding files missing. Run embed_psds.py and redeploy.")

    _META = json.loads(EMB_META.read_text(encoding="utf-8"))
    n, d = int(_META["count"]), int(_META["dim"])
    raw = np.fromfile(EMB_BIN, dtype=np.float32).reshape(n, d)

    norms = np.linalg.norm(raw, axis=1, keepdims=True)
    norms[norms == 0] = 1
    _VECTORS = (raw / norms).astype(np.float32)   # pre-normalise once

    api_key = (os.environ.get("VOYAGE_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("VOYAGE_API_KEY not set in Vercel environment")
    _CLIENT = voyageai.Client(api_key=api_key)


def _normalise_outcome(outcome: str) -> str:
    s = (outcome or "").lower()
    if s.startswith("recommended") and not s.startswith("not"):
        return "recommended"
    if s.startswith("not"):
        return "not"
    if s == "deferred":
        return "deferred"
    return s


def _search(query: str, limit: int, filter_outcome: str | None, filter_therapy: str | None) -> dict:
    import numpy as np
    _ensure_loaded()

    # Embed the query (input_type='query' for asymmetric retrieval)
    r = _CLIENT.embed(
        texts=[query],
        model=_META["model"],
        input_type="query",
        truncation=True,
    )
    qv = np.asarray(r.embeddings[0], dtype=np.float32)
    qn = np.linalg.norm(qv) or 1.0
    qv = qv / qn

    # Cosine similarity (vectors are pre-normalised → just a dot product)
    sims = _VECTORS @ qv

    # Build candidate list with metadata; apply filters
    drugs = _META["drugs"]
    f_out = filter_outcome.lower() if filter_outcome else None
    f_ta  = filter_therapy.lower() if filter_therapy else None

    scored = []
    for i, score in enumerate(sims):
        meta = drugs[i]
        if f_out and _normalise_outcome(meta.get("outcome", "")) != f_out:
            continue
        if f_ta and (meta.get("therapy_area", "").lower() != f_ta):
            continue
        scored.append((float(score), i))

    scored.sort(key=lambda x: -x[0])
    top = scored[: max(1, min(limit, 50))]

    results = []
    for score, idx in top:
        m = drugs[idx]
        results.append({
            "drug":       m.get("name"),
            "score":      round(float(score), 4),
            "year":       m.get("year"),
            "indication": m.get("indication"),
            "outcome":    m.get("outcome"),
        })
    return {
        "query":   query,
        "model":   _META["model"],
        "count":   len(results),
        "results": results,
    }


# ── Vercel handler ────────────────────────────────────────────────────────────
class handler(BaseHTTPRequestHandler):

    def _send(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")          # CORS so the static site can call it
        self.send_header("Cache-Control", "public, max-age=120")     # cache identical queries 2 min
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
        f_ta  = (qs.get("filter_therapy", [""])[0] or None)

        try:
            payload = _search(q, limit, f_out, f_ta)
            return self._send(200, payload)
        except Exception as e:
            return self._send(500, {"error": str(e)[:240]})

    def do_OPTIONS(self):
        # Pre-flight CORS
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
