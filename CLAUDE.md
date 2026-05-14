# script.report

> Australian medicine subsidy tracker — a static dashboard of every PBAC Public
> Summary Document (PSD) and the PBS expenditure that follows. Built from public
> data on pbs.gov.au. Lives at https://script.report (Vercel).

This file is the project's persistent context for Claude Code. Skim it before
making changes — the conventions and "things to avoid" sections matter.

---

## Stack

- **Frontend**: single-file dashboard `index.html` (~3,000 lines, vanilla
  HTML/CSS/JS). Plus `site_data.js` — an auto-generated `window.SITE_DATA = {...}`
  payload that the dashboard reads at load time. **Never hand-edit `site_data.js`.**
- **Charting**: Chart.js v4 loaded from cdnjs at runtime. Bar/donut/line charts.
  Some bars are pure CSS/HTML.
- **Fonts**: Playfair Display (display headings), Lora (body), system sans (UI).
  Editorial newspaper feel.
- **Colour tokens**: red `#C62828` on cream `#FDFBF7`. Dark mode supported via
  `data-theme="dark"` attribute on `<html>`. Theme persists via localStorage.
- **Backend**: a single Python serverless function on Vercel at `api/search.py`
  for semantic precedent search (Voyage AI embeddings, in-memory cosine
  similarity over a ~12 MB float32 binary).
- **Pipeline**: Python package `script_report/` with subpackages for scrapers,
  extractors, embedders, parsers, and the site builder. Dispatched via
  `python -m script_report <command>`.
- **Data extraction**: Claude Haiku (model `claude-haiku-4-5-20251001`) used by
  `script_report.extractors.psd_extractor` to turn PSD text into structured
  CSV fields.
- **Embeddings**: Voyage AI (`voyage-3` by default) used by
  `script_report.embedders.voyage_embedder` to build per-drug "decision profile"
  vectors for similar-drug recommendations (with an ATC-prefix tiebreaker)
  and free-text semantic search.

---

## Data pipeline

Run in order. Each step is `--resume`-aware where applicable, so re-running
only does new work.

```
1.  python -m script_report download              →  data/psds/*.pdf  +  data/psds/*.html
2.  python -m script_report extract --resume      →  data/psd_extracted.csv
3.  python -m script_report spend                 →  data/pbs_drug_spend.csv
4.  python -m script_report schedule              →  data/pbs_schedule_atc.csv (ATC backfill)
5.  python -m script_report atc                   →  data/atc_benefit.csv  +  data/atc_services.csv
6.  python -m script_report calendar              →  data/pbac_calendar.json   (cycle-timeframe PDFs)
7.  python -m script_report agendas               →  data/pbac_agendas.json    (upcoming meeting agendas)
8.  python -m script_report outcomes              →  data/pbac_outcomes.json   (post-meeting outcomes summaries — bridge between agendas and PSDs)
9.  python -m script_report brandmap              →  data/brand_to_generic.json (Smart Search brand normalisation)
10. python -m script_report embed --resume        →  data/psd_embeddings.bin
                                                   +  data/psd_embeddings_meta.json
                                                   +  data/psd_nearest.json
11. python -m script_report map                   →  data/psd_map.json   (UMAP 2D projection for the cosmos plot)
12. python -m script_report build                 →  site_data.js
13. vercel --prod  (or git push)
```

`python -m script_report refresh` orchestrates 1–9 with flags:
- `--build-only`   skip downloads/extract/embed, just rebuild site_data.js
- `--no-psds`      skip PSD downloading
- `--no-embed`     skip Voyage embedding
- `--no-deploy`    build but don't push to Vercel

---

## Key files

| Path | What |
|------|------|
| `index.html` | The dashboard (vanilla HTML/CSS/JS, served at `/` on Vercel). |
| `site_data.js` | **Auto-generated.** Do not edit. Run `python -m script_report build`. |
| `pyproject.toml` | Package metadata + deps. `pip install -e .` for editable install. |
| `script_report/__main__.py` | CLI dispatcher (`python -m script_report <command>`). |
| `script_report/config.py` | Paths, model identifiers, batch sizes, pricing. |
| `script_report/refresh.py` | Full pipeline orchestrator (direct calls, not subprocess). |
| `script_report/data/loaders.py` | Per-input loaders consumed by the site builder. |
| `script_report/builders/site_builder.py` | Composes `site_data.js` from loader output. |
| `script_report/scrapers/psd_downloader.py` | Polite PBAC PSD scraper (fingerprint dedup). |
| `script_report/scrapers/pbs_spend.py` | PBS expenditure Excel fetcher. |
| `script_report/scrapers/pbs_schedule.py` | Monthly PBS Schedule API CSV bundle → ATC backfill. |
| `script_report/extractors/psd_extractor.py` | Haiku-powered field extraction (PDFs + HTML PSDs). |
| `script_report/extractors/agenda_extractor.py` | Pulls upcoming PBAC + Intracycle meeting agendas (URLs in `data/agenda_sources.json`) → `data/pbac_agendas.json` via Haiku. |
| `script_report/extractors/outcomes_extractor.py` | Pulls the short "Recommendations made by the PBAC" outcomes PDFs (URLs in `data/outcomes_sources.json`) → `data/pbac_outcomes.json` via Haiku. Drives the homepage "Just decided" panel. |
| `script_report/embedders/voyage_embedder.py` | Voyage embeddings + nearest table (ATC tiebreaker). |
| `script_report/parsers/atc_parser.py` | PBS ATC HTML-as-XLS parser. |
| `script_report/parsers/pbac_calendar.py` | PBS Cycle Timeframe PDF parser. |
| `data/agenda_sources.json` | Hand-curated list of upcoming PBAC / Intracycle agenda URLs. New ones get pasted in ~6 weeks before each meeting. |
| `data/outcomes_sources.json` | Hand-curated list of PBAC outcomes URLs (the short post-meeting summaries). New ones get pasted in after each meeting; entries auto-retire once the matching PSDs land. |
| `script_report/utils/helpers.py` | MONTH_MAP, data_path resolver, .env loader. |
| `script_report/utils/similarity.py` | ATC-prefix tiebreaker for cosine ties. |
| `script_report/utils/drug_names.py` | Drug-name normalisation + salt-strip / multi-drug split candidate keys. |
| `api/search.py` | Vercel Python function. Loads embeddings binary at cold start, embeds queries, returns top-N by cosine. |
| `api/requirements.txt` | Function deps: `voyageai`, `numpy`. |
| `vercel.json` | Function config + headers. |
| `.vercelignore` | Excludes 3,000 source PDFs and other heavy local-only files from deployment. |
| `.env` | Local secrets (gitignored). Contains `ANTHROPIC_API_KEY`, `VOYAGE_API_KEY`. |

---

## Deployment

- Vercel project: `hecon-watch` (legacy name; can't rename without breaking).
- Domain: `script.report` (configured in Vercel → Domains).
- Auto-deploy on git push to main.
- **Required env vars in Vercel** (Project Settings → Environment Variables):
  - `VOYAGE_API_KEY` — Voyage embeddings, used by `api/search.py`.
  - `ANTHROPIC_API_KEY` — Claude Haiku for query parsing + grounded synthesis.
  - `SMART_SEARCH_V2` *(optional kill switch)* — set to `0` to disable the
    parser + synthesis path and revert `api/search.py` to legacy semantic-only
    behaviour without redeploying.
- Static files served from project root. `index.html` is served at `/` by
  default (no rewrites needed after the v0.11 rename).
- Function memory 512 MB, max duration 10 s.

---

## Conventions

- **Australian-only focus.** NICE / UK comparisons were intentionally removed
  in May 2026. Do not re-add.
- **No AI-generated editorial content.** No blog articles, "Latest analysis"
  cards, fake testimonials, or AI-written commentary. Dashboard is data first.
  *Narrow exception:* Smart Search emits a grounded one-paragraph synthesis
  above the result cards (see `api/_synthesis.py`). It must cite `(drug, year)`
  for every claim and returns `null` rather than producing filler. Anything
  outside that pattern is not allowed.
- **Brand wordmark**: `script.<em>report</em>` (lowercase with period; "report"
  in red). Logo mark is a small SVG line chart with a red pulsing endpoint.
  Tagged with `AU` next to the wordmark.
- **Dashboard sections** roughly in order: page header → search → KPI strip →
  PBS spending chart (with toggleable ATC class lines) → outcomes donut +
  submissions/year → top spend + cost-per-script → ATC + therapy-area volume →
  ICER distribution + persistence → therapy-area outcomes → browse table.
- **Drug detail page** order: header chips → KPI strip → cost-effectiveness →
  PBS expenditure → similar precedents → clinical evidence → decision context
  → rejection reasons (if applicable) → submission history → external links.
- **Lay-friendly language**. The site explains PBAC the first time it appears
  in the lede; acronyms have glossary tooltips (HTA / PBAC terms — ICER, QALY,
  RSA, PSCR, ESC, DUSC, etc.).

---

## Current state (May 2026)

**Working:**
- Full Australian dashboard (KPI strip, all charts, browse table).
- Drug detail pages with all 25+ extracted PSD fields surfaced, plus:
  - Comparator + trial backlinks
  - Resubmission story (per-drug deltas across attempts)
  - Cost-basis label (Cost-min / Dominant / Redacted / ICER value)
- Similar Drugs panel (precomputed nearest-neighbours from Voyage).
- Semantic precedent search at `/api/search` — UI mode toggle in the search
  bar, sentence-style suggestions, loading + error states, in-session cache.
  Filter chips for outcome (server-side), year (client-side), and therapy area
  (client-side, top six therapy areas built from `SITE_DATA.psd.drugs`).
- About / methodology page at `#/about` — data sources, extraction approach,
  precedent-search explanation, limitations, contact. Linked from the header
  nav and the footer "Sections" column.
- Homepage analytical panels:
  - Recently-published feed (top-left, 2/3 width)
  - "Coming up at PBAC" agenda panel (top-right, 1/3 width) — upcoming
    PBAC + Intracycle meetings, drugs/indications on the docket, with
    backlinks to existing drug pages where the drug is already in the
    extracted set
  - "Just decided" panel (top-right, above the agenda panel) — recent
    PBAC outcomes from the short post-meeting summary PDFs, bridging the
    6–8 week gap between a meeting and the full PSDs landing. Each item
    shows drug + indication + outcome chip (Recommended / Not recommended
    / Deferred / Withdrawn). Items auto-retire on the build side once the
    matching PSD enters the corpus.
  - "Earned their listing" — drugs that succeeded after rejection
  - Time-to-listing (first-try success rate, multi-attempt median, by-year trend)
  - Cost-basis labels in the browse table + recent feed
- Hash routing: shareable per-drug URLs (`#/drug/<name>`).
- Glossary tooltips on HTA / PBAC acronyms.
- Open Graph meta + inline SVG favicon + branded `og.png`.
- Vercel Web Analytics wired in.
- PBAC cycle calendar wired (Next deadline header pill + upcoming-meetings panel).
- Both PDF and HTML PSDs are captured and extracted.
- ATC code backfill from PBS Schedule monthly CSV bundle.
- Dark mode toggle.
- Deployed to script.report.

**Not yet built:**
- Standalone Contact page (footer / About-page mailto is the contact channel
  for now).
- ATC-class filter on semantic search (would need ATC code in the embeddings
  meta — currently outcome / year / therapy only).

---

## Things to avoid

- **Don't hand-edit `site_data.js`** — regenerated by `python -m script_report build`.
- **Don't re-add NICE / UK comparison content** — intentionally removed.
- **Don't add AI-generated blog articles, "latest analysis" cards, or fake
  testimonials** — site is data-only. (The Smart Search synthesis block is
  the single, grounded exception — see the AI-content note above.)
- **Don't deploy `data/psds/`** — `.vercelignore` excludes it. Those 3,000
  source PDFs are not needed at runtime; the dashboard links straight to
  pbs.gov.au.
- **Don't commit `.env`** — gitignored. Keys go in Vercel env vars for
  production.
- **Don't break the Australian focus.** No global comparisons, no
  international HTA agencies, no UK pricing. Just AU.
- **Don't break the search-mode toggle.** Keyword and Precedent are two
  intentionally distinct UX modes; merging them is tempting but loses signal.

---

## Common commands

```bash
# Install the package (once)
pip install -e .

# Add new PSDs and rebuild everything
python -m script_report refresh

# Just rebuild site_data.js after manual CSV edits
python -m script_report build

# Embed only newly added drugs (cheap; ATC tiebreaker applied automatically)
python -m script_report embed --resume

# Test the live semantic search endpoint
curl "https://script.report/api/search?q=immature+OS+oncology+rejected&limit=5"

# Local dashboard preview (any static server works)
python -m http.server 8000        # then open localhost:8000/

# Manual Vercel deploy (alternative to git push)
vercel --prod
```

---

## Useful URLs

- Master PSD index (source of truth):
  https://www.pbs.gov.au/info/industry/listing/elements/pbac-meetings/psd/public-summary-documents-by-product
- PBS expenditure stats:
  https://www.pbs.gov.au/info/statistics/expenditure-prescriptions/expenditure-and-prescriptions-twelve-months
- PBAC meetings landing page:
  https://www.pbs.gov.au/info/industry/listing/elements/pbac-meetings
- TGA ARTG (referenced from drug detail pages):
  https://www.tga.gov.au/resources/artg

---

## When in doubt

- Read `index.html` to understand the rendered surface area.
- Read `script_report/builders/site_builder.py` (and the loaders it imports)
  to understand the data shape that the dashboard receives.
- Read `script_report/extractors/psd_extractor.py`'s `USER_PROMPT_TEMPLATE`
  to see exactly what fields are extracted from each PSD.
- The `data/` folder is the integration boundary — every script writes there,
  the dashboard reads from there (via site_data.js).
