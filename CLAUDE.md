# script.report

> Australian medicine subsidy tracker — a static dashboard of every PBAC Public
> Summary Document (PSD) and the PBS expenditure that follows. Built from public
> data on pbs.gov.au. Lives at https://script.report (Vercel).

This file is the project's persistent context for Claude Code. Skim it before
making changes — the conventions and "things to avoid" sections matter.

---

## Stack

- **Frontend**: single-file dashboard `site_preview.html` (~3,000 lines, vanilla
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
- **Data extraction**: Claude Haiku (model `claude-haiku-4-5-20251001`) used by
  `extract_psd_text.py` to turn PSD text into structured CSV fields.
- **Embeddings**: Voyage AI (`voyage-3` by default) used by `embed_psds.py` to
  build per-drug "decision profile" vectors for similar-drug recommendations
  and free-text semantic search.

---

## Data pipeline

Run in order. Each script is `--resume`-aware where applicable, so re-running
only does new work.

```
1.  download_missing_psds.py   →  data/psds/*.pdf  +  data/psds/*.html
2.  extract_psd_text.py --resume  →  data/psd_extracted.csv
3.  fetch_pbs_drug_spend.py    →  data/pbs_drug_spend.csv
4.  parse_atc_data.py          →  data/atc_benefit.csv  +  data/atc_services.csv
5.  parse_pbac_calendar.py     →  data/pbac_calendar.json   (cycle-timeframe PDFs)
6.  embed_psds.py --resume     →  data/psd_embeddings.bin
                                 +  data/psd_embeddings_meta.json
                                 +  data/psd_nearest.json
7.  build_site_data.py         →  site_data.js
8.  vercel --prod  (or git push)
```

`refresh.py` orchestrates 1–7 with flags:
- `--build-only`   skip downloads/extract/embed, just rebuild site_data.js
- `--no-psds`      skip PSD downloading
- `--no-embed`     skip Voyage embedding (saves ~$0 incremental, but useful)
- `--no-deploy`    build but don't push to Vercel

---

## Key files

| Path | What |
|------|------|
| `site_preview.html` | The dashboard. Renamed to `index.html` on Vercel. |
| `site_data.js` | **Auto-generated.** Do not edit. Run `build_site_data.py`. |
| `build_site_data.py` | Orchestrator that reads every CSV/JSON and produces `site_data.js`. |
| `download_missing_psds.py` | Polite scraper. Uses fingerprint dedup so it only downloads new PSDs. Output goes to `data/psds/`. |
| `extract_psd_text.py` | Haiku-powered field extraction. Reads PDFs and HTML PSDs. Writes `data/psd_extracted.csv`. |
| `embed_psds.py` | Voyage embeddings + precomputed nearest-neighbours table. |
| `parse_pbac_calendar.py` | One-shot parser for `PBS-Cycle-timeframe-*.pdf` files. |
| `api/search.py` | Vercel Python function. Loads embeddings binary at cold start, embeds queries, returns top-N by cosine. |
| `api/requirements.txt` | Function deps: `voyageai`, `numpy`. |
| `vercel.json` | Function config + headers. |
| `.vercelignore` | Excludes 3,000 source PDFs and other heavy local-only files from deployment. |
| `.env` | Local secrets (gitignored). Contains `ANTHROPIC_API_KEY`, `VOYAGE_API_KEY`. |
| `refresh.py` | Full pipeline runner. |

---

## Deployment

- Vercel project: `hecon-watch` (legacy name; can't rename without breaking).
- Domain: `script.report` (configured in Vercel → Domains).
- Auto-deploy on git push to main.
- **Required env vars in Vercel** (Project Settings → Environment Variables):
  - `VOYAGE_API_KEY` — for `api/search.py`. Set in Production scope.
- Static files served from project root. The `rewrites` rule in `vercel.json`
  serves `site_preview.html` at `/`.
- Function memory 512 MB, max duration 10 s.

---

## Conventions

- **Australian-only focus.** NICE / UK comparisons were intentionally removed
  in May 2026. Do not re-add.
- **No AI-generated blog content.** Dashboard is data + insights only. The
  three "Latest analysis" article cards from the original design were removed.
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
  in the lede; acronyms should ideally have tooltips (not yet done — see
  pending work).

---

## Current state (May 2026)

**Working:**
- Full Australian dashboard (KPI strip, all charts, browse table).
- Drug detail pages with all 25+ extracted PSD fields surfaced.
- Similar Drugs panel (precomputed nearest-neighbours from Voyage).
- Semantic precedent search at `/api/search` — UI mode toggle in the search
  bar, sentence-style suggestions, loading + error states, in-session cache.
- Both PDF and HTML PSDs are captured and extracted.
- Dark mode toggle.
- Deployed to script.report.

**Built but not yet wired into the dashboard:**
- `data/pbac_calendar.json` — parser works, schema produced by
  `parse_pbac_calendar.py`. Needs a "Next deadline" KPI cell + upcoming-meeting
  timeline panel on the dashboard.

**Not yet built:**
- Per-drug shareable URLs (hash routing — `#/drug/<name>`).
- About / Methodology page.
- Contact page.
- Open Graph meta tags + a real favicon.
- Glossary tooltips on acronyms (ICER, QALY, RSA, PSCR, ESC, DUSC, etc.).
- Filters on semantic search results (the API already supports
  `&filter_outcome=`; no UI for it yet).
- Snippet highlighting on semantic results (showing which sentence matched).
- Email alert signup (Substack/Beehiiv embed).

---

## Things to avoid

- **Don't hand-edit `site_data.js`** — regenerated by `build_site_data.py`.
- **Don't re-add NICE / UK comparison content** — intentionally removed.
- **Don't add AI-generated blog articles, "latest analysis" cards, or fake
  testimonials** — site is data-only.
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
# Add new PSDs and rebuild everything
python refresh.py

# Just rebuild site_data.js after manual CSV edits
python build_site_data.py

# Embed only newly added drugs (cheap)
python embed_psds.py --resume

# Test the live semantic search endpoint
curl "https://script.report/api/search?q=immature+OS+oncology+rejected&limit=5"

# Local dashboard preview (any static server works)
python -m http.server 8000        # then open localhost:8000/site_preview.html

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

- Read `site_preview.html` to understand the rendered surface area.
- Read `build_site_data.py` to understand the data shape that the dashboard
  receives.
- Read `extract_psd_text.py`'s `USER_PROMPT_TEMPLATE` to see exactly what fields
  are extracted from each PSD.
- The `data/` folder is the integration boundary — every script writes there,
  the dashboard reads from there (via site_data.js).
