"""
embed_psds.py
━━━━━━━━━━━━━
Builds vector embeddings for every drug in data/psd_extracted.csv using Voyage AI,
plus a precomputed nearest-neighbours table. Powers two features:
  1. Free-text precedent search   (via api/search.py at query time)
  2. "Similar drugs" navigation   (precomputed at build time, no API at runtime)

WHAT GETS EMBEDDED
For each drug we build a single "decision profile" string from the most
salient fields in the extracted CSV:

  drug · brand · therapy area · indication · PICO · line of therapy
       · recommendation · listing type · comparator
       · evidence type · trial size · primary endpoint · economic model
       · ICER range · risk-sharing · rejection reasons · key trials

Then send all profiles through Voyage's embedding API in batches.

OUTPUTS (in data/)
  psd_embeddings.bin        — packed float32 array, shape (N, dim), C-order
  psd_embeddings_meta.json  — { model, dim, count, drugs: [{name, profile_excerpt}, ...] }
  psd_nearest.json          — { drug_name: [neighbour_drug_name, ...], ... }   top-20

REQUIREMENTS
  pip install voyageai numpy python-dotenv --break-system-packages

  Add VOYAGE_API_KEY to .env (sign up free at https://www.voyageai.com).

USAGE
  python embed_psds.py                        # build everything
  python embed_psds.py --resume               # only embed drugs not already in meta
  python embed_psds.py --model voyage-3-lite  # cheaper/smaller model
  python embed_psds.py --top-k 30             # widen the NN table
  python embed_psds.py --dry-run              # estimate cost without calling the API
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
DATA = HERE / "data"
DATA.mkdir(exist_ok=True)

# Optional .env loader
try:
    from dotenv import load_dotenv
    load_dotenv(HERE / ".env")
except ImportError:
    pass

# Required deps
try:
    import numpy as np
except ImportError:
    print("Missing: pip install numpy --break-system-packages")
    sys.exit(1)

try:
    import voyageai
except ImportError:
    print("Missing: pip install voyageai --break-system-packages")
    sys.exit(1)


CSV_PATH       = DATA / "psd_extracted.csv"
EMB_BIN        = DATA / "psd_embeddings.bin"
EMB_META       = DATA / "psd_embeddings_meta.json"
NEAREST_JSON   = DATA / "psd_nearest.json"

# Default Voyage model — voyage-3 balances quality + cost. Lite is cheaper/smaller.
DEFAULT_MODEL  = "voyage-3"
BATCH_SIZE     = 64           # Voyage handles up to 128 inputs per call
PRICE_PER_1M = {              # rough current Voyage pricing (USD)
    "voyage-3":         0.06,
    "voyage-3-lite":    0.02,
    "voyage-3-large":   0.18,
    "voyage-large-2":   0.12,
}


# ── CSV ingestion ─────────────────────────────────────────────────────────────
def latest_per_drug() -> list[dict]:
    """Group rows by drug, return the most recent (year+month) successful row per drug."""
    if not CSV_PATH.exists():
        print(f"Missing {CSV_PATH}. Run extract_psd_text.py first.")
        sys.exit(1)
    by_drug: dict[str, list[dict]] = {}
    with open(CSV_PATH, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("extraction_ok") != "yes":
                continue
            name = (row.get("drug") or "").strip().lower()
            if not name:
                continue
            by_drug.setdefault(name, []).append(row)

    def _sort_key(r):
        try:
            return int(r.get("pbac_year") or 0) * 100 + int(r.get("pbac_month") or 0)
        except (TypeError, ValueError):
            return 0

    drugs = []
    for name, rows in by_drug.items():
        rows.sort(key=_sort_key)
        latest = rows[-1]
        latest["_drug_name"] = name
        latest["_submission_count"] = len(rows)
        drugs.append(latest)
    return drugs


def _f(row: dict, key: str, prefix: str = "") -> str:
    v = (row.get(key) or "").strip()
    if not v or v.lower() in ("none", "null", "n/a"):
        return ""
    return f"{prefix}{v}"


def build_profile(row: dict) -> str:
    """Single string capturing the salient features of a PBAC decision."""
    name = (row.get("drug") or "").strip()
    brand = (row.get("brand_name") or "").strip()
    yr = (row.get("pbac_year") or "").strip()
    mo = (row.get("pbac_month") or "").strip()

    # ICER → human readable
    icer_low  = (row.get("icer_low")  or "").strip()
    icer_high = (row.get("icer_high") or "").strip()
    icer_str = ""
    if icer_low and icer_high:
        try:
            lo = int(float(icer_low)); hi = int(float(icer_high))
            icer_str = f"ICER ${lo//1000}k–${hi//1000}k AUD/QALY"
        except ValueError:
            pass
    elif icer_low or icer_high:
        try:
            v = int(float(icer_low or icer_high))
            icer_str = f"ICER ~${v//1000}k AUD/QALY"
        except ValueError:
            pass

    parts = [
        name + (f" ({brand})" if brand else ""),
        _f(row, "therapy_area",      "Therapy area: "),
        _f(row, "indication",        "Indication: "),
        _f(row, "pico_population",   "Population: "),
        _f(row, "line_of_therapy",   "Line of therapy: "),
        _f(row, "recommendation",    "PBAC outcome: ") + (f" ({yr}-{mo.zfill(2)})" if yr else ""),
        _f(row, "listing_type",      "Listing type: "),
        _f(row, "comparator",        "Comparator: "),
        _f(row, "evidence_type",     "Evidence: "),
        (f"Trial size: {row['trial_size']}" if (row.get("trial_size") or "").strip() else ""),
        _f(row, "primary_endpoint",  "Primary endpoint: "),
        _f(row, "economic_model",    "Economic model: "),
        icer_str,
        ("Risk-sharing arrangement: " + (row.get("risk_sharing_note") or "yes")) if (row.get("risk_sharing") or "").lower() == "yes" else "",
        _f(row, "rejection_reasons", "PBAC concerns: "),
        _f(row, "key_trials",        "Trials: "),
    ]
    profile = ". ".join([p for p in parts if p])

    # Defensive cap so a runaway extraction doesn't burn tokens
    if len(profile) > 4000:
        profile = profile[:4000] + "…"
    return profile


# ── Embedding ────────────────────────────────────────────────────────────────
def estimate_tokens(texts: list[str]) -> int:
    # Voyage's tokeniser is roughly 4 chars/token for English; this is a rough upper bound
    return sum(max(1, len(t) // 4) for t in texts)


_PERMANENT_ERROR_HINTS = (
    "unauthorized", "401", "invalid api key", "forbidden", "403",
    "bad request", "invalid model", "model_not_found", "not found",
    "validation error", "invalid_argument",
)


def embed_batch(client, texts: list[str], model: str) -> list[list[float]]:
    """Send a batch to Voyage. Retries transient errors; surfaces permanent ones."""
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            # `truncation` defaults to True in the SDK; omitted here for compatibility
            # with older voyageai versions that don't accept the kwarg.
            r = client.embed(texts=texts, model=model, input_type="document")
            return r.embeddings
        except Exception as e:
            last_error = e
            err_type = type(e).__name__
            err_msg  = str(e)
            err_lower = err_msg.lower()
            # Permanent errors — fail loudly, don't waste retries
            if any(h in err_lower for h in _PERMANENT_ERROR_HINTS):
                print(f"\n    Permanent {err_type}: {err_msg}\n")
                raise RuntimeError(
                    f"Voyage API permanent error ({err_type}): {err_msg}"
                ) from e
            wait = min(60, 5 * (2 ** attempt))
            print(f"    [attempt {attempt+1}/5] {err_type}: {err_msg[:240]}")
            print(f"      retrying in {wait}s…")
            time.sleep(wait)
    raise RuntimeError(
        f"Voyage API failed after 5 retries. Last error: "
        f"{type(last_error).__name__}: {last_error}"
    )


# ── Output writers ───────────────────────────────────────────────────────────
def write_outputs(drugs: list[dict], vectors: np.ndarray, model: str, top_k: int):
    n, d = vectors.shape
    print(f"  Writing {n} × {d} float32 vectors → {EMB_BIN}")
    vectors.astype(np.float32).tofile(EMB_BIN)

    meta = {
        "model": model,
        "dim": int(d),
        "count": int(n),
        "input_type_documents": "document",
        "input_type_queries":   "query",
        "drugs": [
            {
                "name":       drugs[i]["_drug_name"],
                "year":       (drugs[i].get("pbac_year") or "").strip(),
                "indication": (drugs[i].get("indication") or "").strip()[:140],
                "outcome":    (drugs[i].get("recommendation") or "").strip(),
            }
            for i in range(n)
        ],
    }
    EMB_META.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"  Wrote meta → {EMB_META}")

    # ── Nearest-neighbour table (cosine similarity) ─────────────────────────
    print(f"  Computing top-{top_k} nearest neighbours per drug…")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1
    unit = vectors / norms
    sim = unit @ unit.T                 # (n, n)
    np.fill_diagonal(sim, -1.0)         # exclude self

    nearest: dict[str, list[dict]] = {}
    names = [d["_drug_name"] for d in drugs]
    for i, name in enumerate(names):
        idxs = np.argpartition(-sim[i], min(top_k, n - 1))[:top_k]
        idxs = idxs[np.argsort(-sim[i, idxs])]
        nearest[name] = [
            {"drug": names[j], "score": float(round(sim[i, j], 4))}
            for j in idxs.tolist() if sim[i, j] > 0
        ]
    NEAREST_JSON.write_text(json.dumps(nearest, indent=2), encoding="utf-8")
    print(f"  Wrote nearest → {NEAREST_JSON}")


# ── Resume support ───────────────────────────────────────────────────────────
def load_existing() -> tuple[np.ndarray | None, dict | None]:
    if not EMB_BIN.exists() or not EMB_META.exists():
        return None, None
    try:
        meta = json.loads(EMB_META.read_text(encoding="utf-8"))
        n, d = int(meta["count"]), int(meta["dim"])
        arr = np.fromfile(EMB_BIN, dtype=np.float32).reshape(n, d)
        return arr, meta
    except Exception as e:
        print(f"  ⚠ Could not load existing embeddings ({e}); starting fresh.")
        return None, None


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Build PSD embeddings via Voyage AI")
    ap.add_argument("--model",  default=DEFAULT_MODEL, help=f"Voyage model (default: {DEFAULT_MODEL})")
    ap.add_argument("--batch",  type=int, default=BATCH_SIZE, help="Batch size")
    ap.add_argument("--top-k",  type=int, default=20,  help="Neighbours to precompute per drug")
    ap.add_argument("--resume", action="store_true",   help="Skip drugs already embedded (matches by name + model)")
    ap.add_argument("--dry-run",action="store_true",   help="Estimate cost without calling the API")
    ap.add_argument("--limit",  type=int, default=0,   help="Embed only first N drugs (testing)")
    args = ap.parse_args()

    # ── Load CSV ─────────────────────────────────────────────────────────────
    print("=" * 65)
    print("PSD Embedder  —  Voyage AI")
    print("=" * 65)
    drugs = latest_per_drug()
    print(f"  Drugs in CSV : {len(drugs):,}")

    if not drugs:
        print("  Nothing to embed — psd_extracted.csv has no rows with extraction_ok=yes.")
        return

    if args.limit:
        drugs = drugs[: args.limit]
        print(f"  Limit applied: {len(drugs):,}")

    profiles = [build_profile(d) for d in drugs]

    # ── Resume? ──────────────────────────────────────────────────────────────
    existing_vec = None
    existing_names_to_idx: dict[str, int] = {}
    if args.resume:
        existing_vec, existing_meta = load_existing()
        if existing_vec is not None and existing_meta and existing_meta.get("model") == args.model:
            existing_names_to_idx = {row["name"]: i for i, row in enumerate(existing_meta["drugs"])}
            print(f"  Existing embeddings: {len(existing_names_to_idx):,}  (model: {args.model})")
        else:
            print("  No reusable existing embeddings (different model or absent).")

    # Decide which to embed
    to_embed_indices = [i for i, d in enumerate(drugs) if d["_drug_name"] not in existing_names_to_idx]
    print(f"  To embed     : {len(to_embed_indices):,}")

    # ── Cost estimate ────────────────────────────────────────────────────────
    tokens = estimate_tokens([profiles[i] for i in to_embed_indices])
    price = PRICE_PER_1M.get(args.model, 0.06)
    print(f"  Est. tokens  : ~{tokens:,}")
    print(f"  Est. cost    : ~${tokens / 1_000_000 * price:.3f}  ({args.model} @ ${price}/1M)")
    print()

    if args.dry_run:
        print("Dry run — no API calls made.")
        return

    if not to_embed_indices and existing_vec is not None:
        print("All drugs already embedded; just refreshing nearest-neighbours table.")
        # Reuse existing vectors in current drug order
        vectors = np.zeros((len(drugs), existing_vec.shape[1]), dtype=np.float32)
        for i, d in enumerate(drugs):
            j = existing_names_to_idx.get(d["_drug_name"])
            if j is not None:
                vectors[i] = existing_vec[j]
        write_outputs(drugs, vectors, args.model, args.top_k)
        return

    # ── Embed in batches ─────────────────────────────────────────────────────
    api_key = (os.environ.get("VOYAGE_API_KEY") or "").strip()
    if not api_key:
        print("ERROR: VOYAGE_API_KEY not set. Sign up free at voyageai.com and add to .env:")
        print("       VOYAGE_API_KEY=pa-...")
        sys.exit(1)
    client = voyageai.Client(api_key=api_key)

    # Pre-flight: tiny test call so we surface auth / model / parameter errors
    # before sending the entire corpus.
    print(f"  Pre-flight: testing {args.model} with a 1-text request…")
    try:
        test = client.embed(texts=["Pre-flight test."], model=args.model, input_type="document")
        if not test.embeddings or not test.embeddings[0]:
            print("  ⚠ Pre-flight returned empty vectors. Aborting.")
            sys.exit(1)
        print(f"  ✓ Pre-flight OK — vector dim = {len(test.embeddings[0])}")
    except Exception as e:
        print()
        print(f"  ✗ Pre-flight failed: {type(e).__name__}: {e}")
        print()
        print("  Common causes:")
        print(f"    • API key invalid          → re-check VOYAGE_API_KEY in .env")
        print(f"    • Model name wrong         → try --model voyage-3-lite or voyage-3-large")
        print(f"    • SDK version too old      → pip install -U voyageai --break-system-packages")
        print(f"    • Free-tier quota exhausted → check usage at https://dash.voyageai.com")
        sys.exit(1)

    # Build the full vectors array, copying existing rows where available
    dim_seen: int | None = (existing_vec.shape[1] if existing_vec is not None else None)
    new_rows: dict[int, np.ndarray] = {}

    for batch_start in range(0, len(to_embed_indices), args.batch):
        batch_idx = to_embed_indices[batch_start: batch_start + args.batch]
        texts = [profiles[i] for i in batch_idx]
        names = [drugs[i]["_drug_name"] for i in batch_idx]
        print(f"  [{batch_start+1:5d}/{len(to_embed_indices)}]  embedding {len(texts)}: "
              f"{', '.join(names[:3])}{', …' if len(names) > 3 else ''}")
        vecs = embed_batch(client, texts, args.model)
        if dim_seen is None:
            dim_seen = len(vecs[0])
        for i, v in zip(batch_idx, vecs):
            new_rows[i] = np.asarray(v, dtype=np.float32)

    # Stitch together: existing rows for already-embedded drugs + freshly embedded rows
    vectors = np.zeros((len(drugs), dim_seen), dtype=np.float32)
    for i, d in enumerate(drugs):
        if i in new_rows:
            vectors[i] = new_rows[i]
        else:
            j = existing_names_to_idx.get(d["_drug_name"])
            if j is not None and existing_vec is not None:
                vectors[i] = existing_vec[j]
            else:
                # Shouldn't happen — every drug is either pre-existing or newly embedded
                print(f"  ⚠ Missing vector for {d['_drug_name']}; will be zeros.")

    write_outputs(drugs, vectors, args.model, args.top_k)
    print()
    print("Next:")
    print("  1. Run build_site_data.py to fold psd_nearest.json into site_data.js")
    print("     (you'll need to add a small loader — see the embed_psds.py docstring)")
    print("  2. Deploy api/search.py to Vercel for runtime semantic search")


if __name__ == "__main__":
    main()
