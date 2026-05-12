"""
script_report.builders.map_builder
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Project the high-dimensional Voyage embeddings down to 2D coordinates
using UMAP and emit ``data/psd_map.json`` for the homepage "PBAC cosmos"
visualisation.

The output is a single JSON file that the site builder folds into
``site_data.js`` so the frontend can render every PBAC decision on a
single canvas without any runtime ML.

Run after ``embed``:
    python -m script_report map
"""

from __future__ import annotations

import json
from pathlib import Path

from script_report.config import DATA_DIR


EMB_BIN  = DATA_DIR / "psd_embeddings.bin"
EMB_META = DATA_DIR / "psd_embeddings_meta.json"
OUT_JSON = DATA_DIR / "psd_map.json"


def _normalise_outcome(s: str) -> str:
    s = (s or "").lower()
    if s.startswith("recommended") and not s.startswith("not"):
        return "recommended"
    if s.startswith("not"):
        return "not"
    if s == "deferred":
        return "deferred"
    if s == "noted":
        return "noted"
    return "other"


def main() -> None:
    import numpy as np
    import umap

    if not EMB_BIN.exists() or not EMB_META.exists():
        raise SystemExit(
            "Missing embeddings. Run `python -m script_report embed` first."
        )

    meta = json.loads(EMB_META.read_text(encoding="utf-8"))
    n, d = int(meta["count"]), int(meta["dim"])
    vecs = np.fromfile(EMB_BIN, dtype=np.float32).reshape(n, d)

    print(f"  UMAP fit: {n} drugs x {d} dim -> 2D")
    reducer = umap.UMAP(
        n_components=2,
        n_neighbors=15,
        min_dist=0.1,
        metric="cosine",
        random_state=42,
    )
    coords = reducer.fit_transform(vecs)

    # Normalise to [0, 1] with a 5% pad on each axis so points don't kiss
    # the edge of the canvas.
    pad = 0.05
    for axis in range(2):
        col = coords[:, axis]
        lo, hi = float(col.min()), float(col.max())
        span = max(hi - lo, 1e-6)
        coords[:, axis] = pad + (1 - 2 * pad) * (col - lo) / span

    drugs = meta["drugs"]
    points = []
    for i in range(n):
        m = drugs[i]
        points.append([
            round(float(coords[i, 0]), 4),
            round(float(coords[i, 1]), 4),
            m.get("name") or "",
            (m.get("therapy_area") or "")[:40],
            _normalise_outcome(m.get("outcome") or ""),
            m.get("year") or "",
        ])

    payload = {
        "schema": ["x", "y", "name", "therapy_area", "outcome_bucket", "year"],
        "n":       n,
        "method":  "umap",
        "params":  {"n_neighbors": 15, "min_dist": 0.1, "metric": "cosine", "random_state": 42},
        "points":  points,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"  Wrote {n} 2D points -> {OUT_JSON.name}")


if __name__ == "__main__":
    main()
