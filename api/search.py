"""
api/search.py
━━━━━━━━━━━━━
Vercel Python serverless function for free-text semantic search over the PSD
embeddings produced by embed_psds.py.

REQUEST
  GET  /api/search?q=<query>&limit=<n>&filter_outcome=<recommended|not|deferred>

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
    """Load vectors and the Voyage client. Idempotent and independent — if either
    fails the function will retry it on the next request, instead of half-caching."""
    global _VECTORS, _META, _CLIENT

    # ── Vectors / metadata ───────────────────────────────────────────────────
    if _VECTORS is None or _META is None:
        import numpy as np
        if not EMB_META.exists() or not EMB_BIN.exists():
            raise RuntimeError(
                "Embedding files missing on the server. Expected "
                f"{EMB_META.name} and {EMB_BIN.name} under data/. "
                "Run embed_psds.py locally then redeploy."
            )
        _META = json.loads(EMB_META.read_text(encoding="utf-8"))
        n, d = int(_META["count"]), int(_META["dim"])
        raw = np.fromfile(EMB_BIN, dtype=np.float32).reshape(n, d)
        norms = np.linalg.norm(raw, axis=1, keepdims=True)
        norms[norms == 0] = 1
        _VECTORS = (raw / norms).astype(np.float32)   # pre-normalise once

    # ── Voyage client (independent of vectors so failures don't poison cache) ─
    if _CLIENT is None:
        import voyageai
        api_key = (os.environ.get("VOYAGE_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError("VOYAGE_API_KEY is not set in Vercel environment variables")
        try:
            client = voyageai.Client(api_key=api_key)
        except Exception as e:
            raise RuntimeError(
                f"voyageai.Client() raised {type(e).__name__}: {e}. "
                f"SDK version: {getattr(voyageai, '__version__', '?')}"
            ) from e
        if client is None or not hasattr(client, "embed"):
            ver = getattr(voyageai, "__version__", "?")
            raise RuntimeError(
                f"voyageai.Client() returned an object without an .embed() method "
                f"(SDK version: {ver}). Pin voyageai>=0.3.0 in api/requirements.txt."
            )
        _CLIENT = client


def _normalise_outcome(outcome: str) -> str:
    s = (outcome or "").lower()
    if s.startswith("recommended") and not s.startswith("not"):
        return "recommended"
    if s.startswith("not"):
        return "not"
    if s == "deferred":
        return "deferred"
    return s


def _search(query: str, limit: int, filter_outcome: str | None) -> dict:
    import numpy as np
    _ensure_loaded()

    if _CLIENT is None or _VECTORS is None or _META is None:
        raise RuntimeError("Search backend failed to initialise — check server logs")

    # Embed the query (input_type='query' for asymmetric retrieval).
    # truncation defaults to True; omitting the kwarg keeps us compatible with
    # older voyageai SDK versions that don't accept it.
    r = _CLIENT.embed(
        texts=[query],
        model=_META["model"],
        input_type="query",
    )
    qv = np.asarray(r.embeddings[0], dtype=np.float32)
    qn = np.linalg.norm(qv) or 1.0
    qv = qv / qn

    # Cosine similarity (vectors are pre-normalised → just a dot product)
    sims = _VECTORS @ qv

    # Outcome filter is normalised on both sides so e.g. "Not recommended" matches "not".
    drugs = _META["drugs"]
    f_out = _normalise_outcome(filter_outcome) if filter_outcome else None

    scored = []
    for i, score in enumerate(sims):
        meta = drugs[i]
        if f_out and _normalise_outcome(meta.get("outcome", "")) != f_out:
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

        try:
            payload = _search(q, limit, f_out)
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
