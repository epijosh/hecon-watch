# script.report

> Australian medicine subsidy tracker — a static dashboard of every PBAC
> Public Summary Document (PSD) and the PBS expenditure that follows.
> Built from public data on pbs.gov.au. Live at https://script.report.

Australia-only focus. No NICE/UK comparisons, no AI-generated commentary —
just the data, structured.

---

## Stack

- **Frontend** — single-file dashboard `index.html` (vanilla HTML/CSS/JS)
  plus `site_data.js`, an auto-generated `window.SITE_DATA = {...}` payload.
- **Charts** — Chart.js v4 from cdnjs.
- **Backend** — one Python serverless function (`api/search.py`) on Vercel
  that does semantic precedent search over Voyage AI embeddings (~12 MB
  float32 binary, in-memory cosine similarity).
- **Pipeline** — Python package `script_report/` with subpackages for
  scrapers, extractors, embedders, parsers, and the site builder.
- **Extraction** — Claude Haiku (`claude-haiku-4-5-20251001`) turns each
  PSD's text into structured CSV fields.
- **Embeddings** — Voyage AI (`voyage-3` by default) builds a per-drug
  "decision profile" vector that powers similar-drug recommendations
  (with an ATC-prefix tiebreaker) and free-text semantic search.

---

## Setup

```bash
# Install the package (editable mode so source edits are picked up live)
pip install -e .

# Local secrets in .env (never committed):
#   ANTHROPIC_API_KEY=...    (extract step)
#   VOYAGE_API_KEY=...       (embed step + api/search.py)

python -m script_report build   # smoke-test the site builder
```

For Vercel, set `VOYAGE_API_KEY` in Project Settings → Environment Variables
(Production scope) so `api/search.py` can answer requests.

---

## CLI

Everything is dispatched through `python -m script_report <command>`:

| Command   | What it does                                                   |
|-----------|----------------------------------------------------------------|
| `build`   | Read every CSV/JSON, write `site_data.js`.                     |
| `refresh` | Full pipeline: download → extract → spend → embed → build → deploy. |
| `download`| Polite scraper of new PBAC PSDs (HTML + PDF).                  |
| `extract` | Haiku-powered field extraction over the PSD corpus.            |
| `spend`   | Fetch PBS drug-level expenditure Excel.                        |
| `atc`     | Parse PBS ATC-class spend / scripts.                           |
| `calendar`| Parse PBS Cycle Timeframe PDFs into `pbac_calendar.json`.      |
| `embed`   | Voyage embeddings + nearest-neighbours table (ATC tiebreaker). |

`refresh` accepts `--build-only`, `--no-psds`, `--no-embed`, `--no-deploy`.

---

## Package layout

```
script_report/
├── __init__.py            # version, package marker
├── __main__.py            # CLI dispatcher (python -m script_report)
├── config.py              # paths, model names, batch sizes, pricing
├── refresh.py             # full-pipeline orchestrator
├── data/
│   └── loaders.py         # load_atc_data / load_pbac_psds / load_psd_extracted /
│                          # load_drug_spend / load_pbac_calendar / load_psd_nearest
├── builders/
│   └── site_builder.py    # writes site_data.js
├── scrapers/
│   ├── psd_downloader.py  # polite PBAC PSD scraper (HTML + PDF)
│   └── pbs_spend.py       # PBS expenditure Excel fetcher
├── extractors/
│   └── psd_extractor.py   # Haiku-powered structured field extraction
├── embedders/
│   └── voyage_embedder.py # Voyage embeddings + nearest table (with ATC tiebreaker)
├── parsers/
│   ├── atc_parser.py      # PBS ATC HTML-as-XLS parser
│   └── pbac_calendar.py   # PBS Cycle Timeframe PDF parser
└── utils/
    ├── helpers.py         # MONTH_MAP, data_path resolver, .env loading
    ├── similarity.py      # ATC-prefix tiebreaker for cosine ties
    └── logging.py         # banner / step helpers
```

`data/` (under the repo root, not in the package) is the I/O boundary —
every script reads and writes there. Both the dashboard (`index.html` →
`site_data.js`) and the Vercel function (`api/search.py` →
`psd_embeddings.bin` / `psd_embeddings_meta.json`) consume from it.

---

## Deployment

- Vercel project: `hecon-watch` (legacy name; domain is `script.report`).
- Auto-deploys on git push to `main`.
- `index.html` is served at `/` by default; no rewrites needed.
- `api/search.py` runs as a Python serverless function (512 MB, 10 s max).
- `.vercelignore` excludes the 3,000 source PDFs in `data/psds/` — they
  aren't needed at runtime; the dashboard links straight to pbs.gov.au.

---

## Conventions

- Australian-only focus. No NICE/UK content, no global comparisons.
- No AI-generated articles or testimonials. Data + insights only.
- Brand wordmark: `script.<em>report</em>` (lowercase, period, "report" red).
- Lay-friendly language. PBAC is explained on first appearance.

## Things to avoid

- Hand-editing `site_data.js` — always run `python -m script_report build`.
- Re-adding NICE/UK comparisons.
- Committing the source PDF folder (`data/psds/`).
- Committing `.env` or any other secret.

---

## Useful URLs

- PSD master index — https://www.pbs.gov.au/info/industry/listing/elements/pbac-meetings/psd/public-summary-documents-by-product
- PBS expenditure stats — https://www.pbs.gov.au/info/statistics/expenditure-prescriptions/expenditure-and-prescriptions-twelve-months
- PBAC meetings landing — https://www.pbs.gov.au/info/industry/listing/elements/pbac-meetings
- PBS calendar / cycle timeframes — https://www.pbs.gov.au/info/industry/useful-resources/pbs-calendar
- TGA ARTG — https://www.tga.gov.au/resources/artg
