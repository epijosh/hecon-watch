"""Pre-render lean SEO landing pages for every drug and sponsor.

Why this exists:
    The main dashboard at script.report/ is a single-page app with hash-routed
    detail views (#/drug/<name>, #/sponsor/<key>). Hash fragments are invisible
    to search crawlers, so ~1,000 pages of unique editorial content (drug
    decisions, sponsor portfolios) currently rank as one page.

    This module emits small static HTML files at /drug/<slug>/index.html and
    /sponsor/<key>/index.html with the SEO-critical content rendered as plain
    HTML (h1, meta description, key facts, JSON-LD), plus a sitemap.xml and
    robots.txt. Crawlers see fully rendered pages; users arriving from a
    search hit see a content-rich landing page with a clear path back to the
    interactive dashboard.

    These pages are deliberately *not* the interactive app — they're SEO
    surfaces. From the dashboard itself, in-page navigation still uses hash
    routing for smooth SPA UX. Static pages link to other static pages so a
    crawler can walk the corpus by following links.

USAGE:
    python -m script_report prerender                # uses current site_data.js
    python -m script_report prerender --clean        # wipe stale drug/ and sponsor/ dirs first
    python -m script_report prerender --limit 20     # quick smoke test
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from script_report.config import REPO_ROOT, SITE_DATA_JS


SITE_URL = "https://script.report"
SITE_NAME = "script.report"
DEFAULT_TAGLINE = "Australian medicine subsidy tracker"


# ── Slugging ────────────────────────────────────────────────────────────────

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9\-]+")
_SLUG_DEDUP_RE = re.compile(r"-{2,}")

# Slugs longer than this get truncated and tagged with a short content hash to
# keep uniqueness. Long slugs blow past Windows' 260-char MAX_PATH limit when
# combined with the repo path prefix, and Google penalises ultra-long URLs.
_SLUG_MAX_LEN = 80


def slugify(s: str) -> str:
    """Turn a drug-name key or sponsor key into a URL-safe slug.

    Examples:
        "pembrolizumab"              → "pembrolizumab"
        "nivolumab plus ipilimumab"  → "nivolumab-plus-ipilimumab"
        "alpha-1 proteinase inhibitor" → "alpha-1-proteinase-inhibitor"
        "merck sharp & dohme"        → "merck-sharp-dohme"
        very-long-name (>80 chars)    → "<first-70-chars>-<8-char-hash>"
    """
    raw = (s or "").lower().strip()
    out = raw.replace("&", "and").replace("/", "-").replace(" ", "-")
    out = _SLUG_STRIP_RE.sub("", out)
    out = _SLUG_DEDUP_RE.sub("-", out).strip("-")
    if len(out) > _SLUG_MAX_LEN:
        # Hash the ORIGINAL string so two near-identical long names don't collide.
        h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        out = out[: _SLUG_MAX_LEN - 9].rstrip("-") + "-" + h
    return out


# ── Outcome / formatting helpers (mirror the JS) ────────────────────────────


def _outcome_bucket(rec: str) -> str:
    s = (rec or "").lower()
    if s.startswith("recommended") and not s.startswith("not"):
        return "rec"
    if s.startswith("not"):
        return "not"
    if s == "deferred":
        return "deferred"
    return "unknown"


_REC_COLOUR = {
    "rec":      "#2e7d32",
    "not":      "#C62828",
    "deferred": "#E65100",
    "unknown":  "#8A7E6E",
}


def _fmt_aud(v: int | float | None) -> str:
    if not v:
        return "—"
    v = float(v)
    if v >= 1e9:
        return f"${v/1e9:.2f}B"
    if v >= 1e6:
        return f"${v/1e6:.0f}M"
    if v >= 1e3:
        return f"${v/1e3:.0f}K"
    return f"${int(v)}"


def _fmt_icer(v: int | float | None) -> str:
    if not v:
        return "—"
    v = float(v)
    if v >= 1e6:
        return f"${v/1e6:.1f}M/QALY"
    if v >= 1e3:
        return f"${v/1e3:.0f}k/QALY"
    return f"${int(v)}/QALY"


_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _fmt_when(year, month) -> str:
    try:
        mo = int(month or 0)
        return f"{_MONTHS[mo]} {year}".strip() if 1 <= mo <= 12 else str(year or "")
    except (ValueError, TypeError, IndexError):
        return str(year or "")


def _title_case(s: str) -> str:
    """Capitalise first letter, leave the rest. Mirrors the JS titleCase()."""
    if not s:
        return ""
    return s[:1].upper() + s[1:]


def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


# ── Shared head / footer fragments ──────────────────────────────────────────


_BASE_CSS = """
body{font-family:'Lora',Georgia,serif;background:#FDFBF7;color:#161008;margin:0;line-height:1.55}
.wrap{max-width:780px;margin:0 auto;padding:32px 22px 64px}
.brand{font-family:'Playfair Display',Georgia,serif;font-size:18px;font-weight:600;letter-spacing:-.2px;margin-bottom:32px}
.brand a{color:inherit;text-decoration:none}
.brand em{font-style:normal;color:#C62828}
.brand .au{font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:10px;font-weight:700;letter-spacing:1.4px;color:#8A7E6E;margin-left:6px;text-transform:uppercase}
.eyebrow{font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:11px;font-weight:700;letter-spacing:1.4px;text-transform:uppercase;color:#C62828;margin-bottom:6px}
h1{font-family:'Playfair Display',Georgia,serif;font-size:36px;font-weight:700;letter-spacing:-.6px;line-height:1.12;margin:0 0 6px}
.brand-sub{font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:13px;color:#8A7E6E;margin-bottom:14px}
.lede{font-size:17px;color:#45392A;font-style:italic;margin:0 0 22px;line-height:1.5}
.kpi-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:#CFC7B2;border:1px solid #CFC7B2;margin:18px 0 26px}
.kpi-cell{background:#FDFBF7;padding:14px 16px}
.kpi-label{font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:10px;font-weight:700;letter-spacing:1.3px;text-transform:uppercase;color:#8A7E6E;margin-bottom:4px}
.kpi-val{font-family:'Playfair Display',Georgia,serif;font-size:24px;font-weight:700;line-height:1.1;letter-spacing:-.3px}
.kpi-sub{font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:11px;color:#8A7E6E;margin-top:3px}
h2{font-family:'Playfair Display',Georgia,serif;font-size:22px;font-weight:700;letter-spacing:-.3px;margin:30px 0 10px}
h3{font-family:'Playfair Display',Georgia,serif;font-size:17px;font-weight:700;letter-spacing:-.2px;margin:22px 0 8px}
.specs{display:grid;grid-template-columns:170px 1fr;gap:4px 18px;margin:10px 0 0;font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:13.5px}
.specs dt{color:#8A7E6E;text-transform:uppercase;font-size:10.5px;font-weight:700;letter-spacing:1.2px;padding-top:5px}
.specs dd{margin:0;padding:3px 0;color:#161008;line-height:1.5}
ul.history,ul.bullets,ul.drug-list{font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:13.5px;padding-left:18px;margin:6px 0 18px;line-height:1.6}
ul.history li,ul.bullets li{margin-bottom:5px}
ul.drug-list{list-style:none;padding-left:0;border-top:1px solid #CFC7B2}
ul.drug-list li{padding:9px 0;border-bottom:1px solid #CFC7B2;display:grid;grid-template-columns:1fr auto auto;gap:14px;align-items:baseline}
ul.drug-list .drug-name{font-family:'Playfair Display',Georgia,serif;font-weight:700;font-size:15px}
ul.drug-list .drug-meta{color:#8A7E6E;font-size:11.5px}
ul.drug-list .drug-spend{font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:12px;font-weight:700;color:#C62828;white-space:nowrap}
ul.drug-list a{color:inherit;text-decoration:none}
ul.drug-list a:hover .drug-name{color:#C62828}
.outcome{font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;padding:2px 8px;border:1px solid currentColor}
table.th-mix{font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:13px;border-collapse:collapse;margin:10px 0 18px;width:100%}
table.th-mix td{padding:6px 8px 6px 0;border-bottom:1px solid #CFC7B2}
table.th-mix .bar{display:inline-block;background:#C62828;opacity:.7;height:7px;vertical-align:middle}
.rej-list{font-size:14px;line-height:1.6;padding-left:0;list-style:none;margin:8px 0 18px}
.rej-list li{padding:5px 0;border-bottom:1px dotted #CFC7B2}
.rej-list .n{display:inline-block;font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:11px;font-weight:700;color:#C62828;background:#F5F1EA;padding:2px 6px;border:1px solid #CFC7B2;margin-right:8px}
.callout{padding:14px 18px;background:#F5F1EA;border-left:3px solid #C62828;margin:18px 0;font-size:14px;line-height:1.55;color:#45392A}
.callout strong{color:#161008}
.muted{color:#8A7E6E}
a{color:#C62828}
a:hover{text-decoration:underline}
.cta{display:inline-block;margin-top:24px;padding:11px 18px;background:#C62828;color:#FDFBF7;text-decoration:none;font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:13px;font-weight:700;letter-spacing:.4px}
.cta:hover{background:#8E1F1F;text-decoration:none}
footer{margin-top:48px;padding-top:18px;border-top:1px solid #CFC7B2;font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:11.5px;color:#8A7E6E;line-height:1.55}
footer a{color:#8A7E6E;text-decoration:none;border-bottom:1px dotted #CFC7B2}
footer a:hover{color:#C62828;border-bottom-color:#C62828}
.foot-links{display:flex;gap:14px;flex-wrap:wrap;margin-top:6px}
@media (max-width:640px){
.kpi-grid{grid-template-columns:1fr 1fr}
.specs{grid-template-columns:1fr;gap:2px}
.specs dd{padding-bottom:8px;border-bottom:1px solid #CFC7B2;margin-bottom:6px}
h1{font-size:28px}
ul.drug-list li{grid-template-columns:1fr;gap:3px}
}
""".strip()


def _head(title: str, description: str, canonical: str, jsonld: dict | None = None) -> str:
    """Render the <head> section. Keep it lean — no font CDN for crawlers."""
    jsonld_block = ""
    if jsonld:
        jsonld_block = (
            f'<script type="application/ld+json">'
            f'{json.dumps(jsonld, ensure_ascii=False, separators=(",", ":"))}'
            f'</script>'
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(title)}</title>
<meta name="description" content="{_esc(description)}">
<link rel="canonical" href="{_esc(canonical)}">
<meta name="theme-color" content="#C62828">
<meta property="og:type" content="article">
<meta property="og:title" content="{_esc(title)}">
<meta property="og:description" content="{_esc(description)}">
<meta property="og:url" content="{_esc(canonical)}">
<meta property="og:site_name" content="{SITE_NAME}">
<meta property="og:image" content="{SITE_URL}/og.png">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{_esc(title)}">
<meta name="twitter:description" content="{_esc(description)}">
<meta name="twitter:image" content="{SITE_URL}/og.png">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' fill='%23FDFBF7'/%3E%3Cpolyline points='4,22 10,16 16,18 22,10 28,12' fill='none' stroke='%23161008' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/%3E%3Ccircle cx='28' cy='12' r='3.5' fill='%23C62828'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@600;700&family=Lora:ital,wght@0,400;0,600;1,400&display=swap" rel="stylesheet">
<style>{_BASE_CSS}</style>
{jsonld_block}
</head>
<body>
<div class="wrap">
<div class="brand"><a href="{SITE_URL}/">script.<em>report</em><span class="au">AU</span></a></div>
"""


def _footer(extra_links: list[tuple[str, str]] | None = None) -> str:
    extras = ""
    if extra_links:
        extras = '<div class="foot-links">' + " ".join(
            f'<a href="{_esc(href)}" target="_blank" rel="noopener">{_esc(label)} →</a>'
            for label, href in extra_links
        ) + "</div>"
    return f"""
<footer>
<p>Part of <a href="{SITE_URL}/">script.report</a> — an independent tracker of every PBAC Public Summary Document and the PBS expenditure that follows. Built from public data on <a href="https://www.pbs.gov.au/" target="_blank" rel="noopener">pbs.gov.au</a>. Not affiliated with the Australian Department of Health, PBAC, or any pharmaceutical company.</p>
{extras}
</footer>
</div>
</body>
</html>
"""


# ── Drug page ───────────────────────────────────────────────────────────────


def _rec_chip(rec: str) -> str:
    bucket = _outcome_bucket(rec)
    colour = _REC_COLOUR[bucket]
    return f'<span class="outcome" style="color:{colour}">{_esc(rec or "—")}</span>'


def _render_drug_page(drug_key: str, drug: dict, drug_spend_by: dict, sponsors_raw: dict | None = None) -> str:
    name = _title_case(drug_key)
    brand = (drug.get("brand_name") or "").strip()
    indication = (drug.get("indication") or "").strip()
    therapy = (drug.get("therapy_area") or "").strip()
    rec = (drug.get("recommendation") or "").strip()
    listing_type = (drug.get("listing_type") or "").strip()
    comparator = (drug.get("comparator") or "").strip()
    submissions = drug.get("submissions") or 1
    first_year = drug.get("first_year") or ""
    latest_year = drug.get("latest_year") or ""
    icer_low = drug.get("icer_low")
    icer_high = drug.get("icer_high")
    icer_note = (drug.get("icer_note") or "").strip()
    cost_basis = drug.get("cost_basis") or "unknown"
    budget = drug.get("budget_impact_aud")
    population = drug.get("population_per_year")
    pico = (drug.get("pico_population") or "").strip()
    evidence = (drug.get("evidence_type") or "").strip()
    endpoint = (drug.get("primary_endpoint") or "").strip()
    line = (drug.get("line_of_therapy") or "").strip()
    model = (drug.get("economic_model") or "").strip()
    trial_size = drug.get("trial_size")
    key_trials = (drug.get("key_trials") or "").strip()
    rejection_reasons = (drug.get("rejection_reasons") or "").strip()
    risk_sharing = drug.get("risk_sharing")
    risk_note = (drug.get("risk_sharing_note") or "").strip()
    history = drug.get("history") or []
    nearest = drug.get("nearest") or []

    canonical = f"{SITE_URL}/drug/{slugify(drug_key)}/"

    # Title + meta description: dense, factual, keyword-rich
    title_bits = [name]
    if brand:
        title_bits.append(f"({brand})")
    title_bits.append("— PBAC decision & PBS expenditure")
    title = " ".join(title_bits)

    desc_bits: list[str] = []
    if rec:
        desc_bits.append(f"PBAC: {rec}")
        if latest_year:
            desc_bits[-1] += f" ({latest_year})"
    if therapy:
        desc_bits.append(therapy)
    if indication:
        desc_bits.append(indication[:90])
    if icer_high or icer_low:
        desc_bits.append(f"ICER {_fmt_icer(icer_high or icer_low)}")
    if submissions > 1:
        desc_bits.append(f"{submissions} PBAC submissions")
    desc = " · ".join(desc_bits)[:240]

    # ── KPI strip ──────────────────────────────────────────────────────────
    rec_colour = _REC_COLOUR[_outcome_bucket(rec)]
    icer_cell_val: str
    icer_cell_sub: str
    if icer_high or icer_low:
        v = icer_high or icer_low
        icer_cell_val = _fmt_icer(v)
        if icer_low and icer_high and icer_low != icer_high:
            icer_cell_sub = f"Range: {_fmt_icer(icer_low)}–{_fmt_icer(icer_high)}"
        else:
            icer_cell_sub = "Base-case"
    else:
        cost_basis_labels = {
            "dominant":         ("Dominant", "less costly + more effective"),
            "cost_neutral":     ("Cost-neutral", "no incremental cost"),
            "cost_minimisation": ("Cost-min", "cost-minimisation analysis"),
            "redacted":         ("Redacted", "commercial-in-confidence"),
            "not_modelled":     ("Not modelled", "no economic evaluation"),
            "unknown":          ("—", "ICER not stated"),
            "numeric":          ("—", ""),  # shouldn't hit
        }
        lbl, sub = cost_basis_labels.get(cost_basis, ("—", ""))
        icer_cell_val = lbl
        icer_cell_sub = sub

    sp = drug_spend_by.get(drug_key.lower()) if drug_spend_by else None
    spend_cell_html = ""
    if sp and sp.get("gov_benefit_aud"):
        spend_cell_html = f"""<div class="kpi-cell">
<div class="kpi-label">PBS spend</div>
<div class="kpi-val" style="color:#C62828">{_fmt_aud(sp.get("gov_benefit_aud"))}</div>
<div class="kpi-sub">{(sp.get("scripts") or 0):,} scripts · {sp.get("report_year", "latest")}</div>
</div>"""
    else:
        spend_cell_html = f"""<div class="kpi-cell">
<div class="kpi-label">Submissions</div>
<div class="kpi-val">{submissions}</div>
<div class="kpi-sub">{f"{first_year} → {latest_year}" if first_year and latest_year else (first_year or latest_year or "")}</div>
</div>"""

    kpi_html = f"""<div class="kpi-grid">
<div class="kpi-cell">
<div class="kpi-label">PBAC outcome</div>
<div class="kpi-val" style="color:{rec_colour}">{_esc(rec or "—")}</div>
<div class="kpi-sub">{_esc(listing_type or (str(latest_year) if latest_year else "—"))}</div>
</div>
<div class="kpi-cell">
<div class="kpi-label">ICER (AUD/QALY)</div>
<div class="kpi-val">{_esc(icer_cell_val)}</div>
<div class="kpi-sub">{_esc(icer_cell_sub)}</div>
</div>
<div class="kpi-cell">
<div class="kpi-label">Submissions</div>
<div class="kpi-val">{submissions}</div>
<div class="kpi-sub">{f"first {first_year}" if first_year else "—"}</div>
</div>
{spend_cell_html}
</div>"""

    # ── Specs block ────────────────────────────────────────────────────────
    specs: list[tuple[str, str]] = []
    if therapy:    specs.append(("Therapy area", _esc(therapy)))
    if line:       specs.append(("Line of therapy", _esc(line)))
    if evidence:   specs.append(("Evidence base", _esc(evidence)))
    if endpoint:   specs.append(("Primary endpoint", _esc(endpoint)))
    if trial_size: specs.append(("Pivotal trial size", f"{int(trial_size):,} patients"))
    if key_trials: specs.append(("Key trials", _esc(key_trials)))
    if comparator: specs.append(("Comparator", _esc(comparator)))
    if model:      specs.append(("Economic model", _esc(model)))
    if budget:     specs.append(("Budget impact", _fmt_aud(budget)))
    if population: specs.append(("Eligible patients/year", f"{int(population):,}"))
    if icer_note:  specs.append(("ICER note", _esc(icer_note)))
    if risk_sharing:
        rs_text = "Risk-sharing arrangement in place"
        if risk_note:
            rs_text += f" — {_esc(risk_note)}"
        specs.append(("Risk sharing", rs_text))
    specs_html = ""
    if specs:
        items = "".join(f"<dt>{label}</dt><dd>{val}</dd>" for label, val in specs)
        specs_html = f'<dl class="specs">{items}</dl>'

    # ── Rejection block ────────────────────────────────────────────────────
    rejection_html = ""
    if rejection_reasons and _outcome_bucket(rec) == "not":
        rejection_html = f"""<h2>Why PBAC said no</h2>
<div class="callout"><strong>Reasons cited in the latest PSD:</strong> {_esc(rejection_reasons)}</div>"""

    # ── Submission history ─────────────────────────────────────────────────
    history_html = ""
    if len(history) > 1:
        items = []
        for h in history:
            yr, mo = h.get("year"), h.get("month")
            r = h.get("rec") or "?"
            lt = h.get("listing_type") or ""
            icer_h_low = h.get("icer_low")
            icer_h_high = h.get("icer_high")
            icer_str = ""
            if icer_h_low or icer_h_high:
                icer_str = f" — ICER {_fmt_icer(icer_h_high or icer_h_low)}"
            items.append(
                f'<li><strong>{_esc(_fmt_when(yr, mo))}</strong>: {_rec_chip(r)}'
                f'{" · " + _esc(lt) if lt else ""}{icer_str}</li>'
            )
        history_html = f'<h2>Submission history</h2><ul class="history">{"".join(items)}</ul>'

    # ── Similar drugs ──────────────────────────────────────────────────────
    similar_html = ""
    if nearest:
        items = []
        for n in nearest[:6]:
            other = n.get("drug")
            if not other:
                continue
            items.append(
                f'<li><a href="/drug/{slugify(other)}/">'
                f'<div class="drug-name">{_esc(_title_case(other))}</div></a>'
                f'<span class="drug-meta">similarity {n.get("score", 0):.2f}</span></li>'
            )
        if items:
            similar_html = f'<h2>Similar precedents</h2><ul class="drug-list">{"".join(items)}</ul>'

    # ── PBS expenditure callout (if not already shown in KPI) ──────────────
    spend_section_html = ""
    if sp and sp.get("gov_benefit_aud"):
        scripts = sp.get("scripts") or 0
        cps = sp.get("cost_per_script_aud")
        spend_section_html = f"""<h2>PBS expenditure</h2>
<div class="callout">
The Australian government paid <strong>{_fmt_aud(sp.get('gov_benefit_aud'))}</strong> in PBS benefits for {_esc(name)} in {_esc(sp.get('report_year', 'the latest reporting year'))} across <strong>{scripts:,}</strong> scripts{f", at <strong>${int(cps):,}</strong> per script" if cps else ""}.
</div>"""

    # ── JSON-LD structured data ────────────────────────────────────────────
    jsonld = {
        "@context": "https://schema.org",
        "@type":    "MedicalEntity",
        "name":     name,
        "url":      canonical,
        "description": desc,
    }
    if brand:
        jsonld["alternateName"] = brand

    # ── Body ──────────────────────────────────────────────────────────────
    lede_bits: list[str] = []
    if rec:
        lede_bits.append(
            f"PBAC's latest decision on {_esc(name)}: <strong>{_esc(rec)}</strong>"
            + (f" ({latest_year})" if latest_year else "")
            + "."
        )
    if indication:
        lede_bits.append(f"Considered for {_esc(indication.rstrip('.'))}.")
    lede = " ".join(lede_bits).strip() if lede_bits else ""

    pico_html = ""
    if pico:
        pico_html = f'<h3>Eligible population</h3><p style="font-size:14px;color:#45392A">{_esc(pico)}</p>'

    body = f"""<div class="eyebrow">{_esc(therapy or "PBAC decision")}</div>
<h1>{_esc(name)}</h1>
{f'<div class="brand-sub">Brand: {_esc(brand)}</div>' if brand else ''}
{f'<p class="lede">{lede}</p>' if lede else ''}
{kpi_html}
{pico_html}
{specs_html}
{rejection_html}
{spend_section_html}
{history_html}
{similar_html}
<a class="cta" href="{SITE_URL}/#/drug/{quote(drug_key, safe='')}">Open on full dashboard →</a>
"""

    extra_links = [
        ("Browse all PSDs on pbs.gov.au", "https://www.pbs.gov.au/info/industry/listing/elements/pbac-meetings/psd"),
        ("PBS Schedule item", f"https://www.pbs.gov.au/medicine/item/{drug_key.replace(' ', '%20')}"),
    ]

    return _head(title, desc, canonical, jsonld) + body + _footer(extra_links)


# ── Sponsor page ────────────────────────────────────────────────────────────


def _render_sponsor_page(sponsor_key: str, det: dict) -> str:
    name = det.get("sponsor", "")
    n_subs = det.get("n_submissions", 0)
    n_drugs = det.get("n_drugs", 0)
    n_listed = det.get("n_drugs_listed", 0)
    n_rec = det.get("n_recommended", 0)
    n_not = det.get("n_not", 0)
    n_def = det.get("n_deferred", 0)
    pct = det.get("pct_recommended")
    spend = det.get("total_spend_aud", 0)
    first_year = det.get("first_year", 0)
    last_year = det.get("last_year", 0)
    top_therapy = det.get("top_therapy_area", "")
    drugs = det.get("drugs") or []
    therapies = det.get("therapy_areas") or []
    by_year = det.get("by_year") or {}
    rejs = det.get("rejection_reasons") or []

    canonical = f"{SITE_URL}/sponsor/{slugify(sponsor_key)}/"

    title = f"{name} — PBAC submissions & PBS spend captured"
    desc_bits = [f"{n_subs} PBAC submissions"]
    if pct is not None:
        desc_bits.append(f"{pct}% recommended")
    desc_bits.append(f"{n_drugs} drugs ({n_listed} currently listed)")
    if spend:
        desc_bits.append(f"{_fmt_aud(spend)} PBS spend captured")
    if top_therapy:
        desc_bits.append(f"mostly {top_therapy}")
    if first_year and last_year:
        desc_bits.append(f"active {first_year}–{last_year}")
    desc = " · ".join(desc_bits)[:240]

    bucketable = n_rec + n_not + n_def
    win_sub = (
        f"{n_rec} recommended of {bucketable} bucketable"
        if bucketable else "no decisive outcomes"
    )
    pct_val_html = f'{pct}<span style="font-size:13px;color:#8A7E6E"> %</span>' if pct is not None else '<span style="font-size:18px;color:#8A7E6E">n/a</span>'
    spend_val_html = _fmt_aud(spend) if spend else '<span style="font-size:18px;color:#8A7E6E">—</span>'

    kpi_html = f"""<div class="kpi-grid">
<div class="kpi-cell">
<div class="kpi-label">Submissions</div>
<div class="kpi-val">{n_subs}</div>
<div class="kpi-sub">{f"{first_year} → {last_year}" if first_year and last_year else ""}</div>
</div>
<div class="kpi-cell">
<div class="kpi-label">Win rate</div>
<div class="kpi-val">{pct_val_html}</div>
<div class="kpi-sub">{_esc(win_sub)}</div>
</div>
<div class="kpi-cell">
<div class="kpi-label">Drugs</div>
<div class="kpi-val">{n_drugs}<span style="font-size:13px;color:#8A7E6E"> · {n_listed} listed</span></div>
<div class="kpi-sub">unique molecules submitted</div>
</div>
<div class="kpi-cell">
<div class="kpi-label">PBS spend captured</div>
<div class="kpi-val" style="color:#C62828">{spend_val_html}</div>
<div class="kpi-sub">FY-latest govt benefit on owned drugs</div>
</div>
</div>"""

    # ── Drug portfolio ─────────────────────────────────────────────────────
    portfolio_html = ""
    if drugs:
        items = []
        for d in drugs[:50]:  # cap at 50 to keep file lean
            dk = d.get("drug", "")
            therapy_meta = d.get("therapy_area", "") or ""
            spend_aud = d.get("spend_aud") or 0
            rec = d.get("latest_rec", "")
            year = d.get("latest_year", "")
            meta_bits: list[str] = []
            if therapy_meta: meta_bits.append(therapy_meta)
            if rec:          meta_bits.append(rec)
            if year:         meta_bits.append(str(year))
            meta = " · ".join(meta_bits)
            spend_str = _fmt_aud(spend_aud) if spend_aud else (
                '<span class="muted" title="spend goes to a different sponsor">other sponsor</span>'
                if not d.get("owned") else "—"
            )
            items.append(
                f'<li><a href="/drug/{slugify(dk)}/">'
                f'<div class="drug-name">{_esc(_title_case(dk))}</div></a>'
                f'<span class="drug-meta">{_esc(meta)}</span>'
                f'<span class="drug-spend">{spend_str}</span></li>'
            )
        portfolio_html = f"""<h2>Drug portfolio</h2>
<p class="muted" style="font-size:12px;margin:0 0 8px">Sorted by PBS spend captured. PBS expenditure is credited to the sponsor with the most recent Recommended decision per drug, so biosimilars and originator brands aren't double-counted.</p>
<ul class="drug-list">{"".join(items)}</ul>"""

    # ── Therapy area mix ───────────────────────────────────────────────────
    therapy_html = ""
    if therapies:
        max_n = therapies[0].get("n", 1)
        rows = []
        for t in therapies[:10]:
            n = t.get("n", 0)
            bar_w = max(2, round(n / max_n * 200))
            rows.append(
                f'<tr><td style="width:180px">{_esc(t.get("area", ""))}</td>'
                f'<td><span class="bar" style="width:{bar_w}px"></span> '
                f'<span style="margin-left:8px;font-weight:700">{n}</span></td></tr>'
            )
        therapy_html = f'<h2>Therapy area mix</h2><table class="th-mix">{"".join(rows)}</table>'

    # ── Year activity (compact text — bars too heavy for static HTML) ─────
    activity_html = ""
    if by_year:
        years = sorted(by_year.keys())
        bits = [f"<strong>{y}</strong> ({by_year[y]})" for y in years]
        activity_html = (
            f'<h2>Activity by year</h2>'
            f'<p style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:12.5px;color:#45392A;line-height:1.9">'
            f'{" · ".join(bits)}</p>'
        )

    # ── Rejection patterns ─────────────────────────────────────────────────
    rej_html = ""
    if rejs and n_not >= 2:
        items = [
            f'<li><span class="n">{r["n"]}×</span>{_esc(r["reason"])}</li>'
            for r in rejs
        ]
        rej_html = f"""<h2>Rejection patterns</h2>
<p class="muted" style="font-size:12px;margin:0 0 8px">Across {n_not} Not recommended decisions on file.</p>
<ul class="rej-list">{"".join(items)}</ul>"""

    jsonld = {
        "@context": "https://schema.org",
        "@type":    "Organization",
        "name":     name,
        "url":      canonical,
        "description": desc,
    }

    lede_bits: list[str] = []
    if n_subs:
        active = ""
        if first_year and last_year and first_year != last_year:
            active = f", active {first_year}–{last_year}"
        elif first_year:
            active = f", first appeared {first_year}"
        lede_bits.append(f"{n_subs} PBAC submission{'' if n_subs == 1 else 's'} on file{active}.")
    if pct is not None:
        lede_bits.append(f"{pct}% have been recommended.")
    lede = " ".join(lede_bits)

    body = f"""<div class="eyebrow">Sponsor</div>
<h1>{_esc(name)}</h1>
{f'<p class="lede">{lede}</p>' if lede else ''}
{kpi_html}
{portfolio_html}
{therapy_html}
{activity_html}
{rej_html}
<a class="cta" href="{SITE_URL}/#/sponsor/{quote(sponsor_key, safe='')}">Open on full dashboard →</a>
"""

    return _head(title, desc, canonical, jsonld) + body + _footer()


# ── Sitemap + robots ────────────────────────────────────────────────────────


def _render_sitemap(drug_urls: list[str], sponsor_urls: list[str], lastmod: str) -> str:
    urls = [
        f'<url><loc>{SITE_URL}/</loc><lastmod>{lastmod}</lastmod><priority>1.0</priority></url>',
        f'<url><loc>{SITE_URL}/about/</loc><lastmod>{lastmod}</lastmod><priority>0.5</priority></url>',
    ]
    for u in drug_urls:
        urls.append(f'<url><loc>{u}</loc><lastmod>{lastmod}</lastmod><priority>0.8</priority></url>')
    for u in sponsor_urls:
        urls.append(f'<url><loc>{u}</loc><lastmod>{lastmod}</lastmod><priority>0.7</priority></url>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</urlset>\n"
    )


def _render_robots() -> str:
    return (
        "User-agent: *\n"
        "Allow: /\n"
        f"Sitemap: {SITE_URL}/sitemap.xml\n"
    )


# ── Orchestration ───────────────────────────────────────────────────────────


def _load_site_data() -> dict:
    """Read window.SITE_DATA out of the generated site_data.js."""
    if not SITE_DATA_JS.exists():
        print(f"ERROR: {SITE_DATA_JS} not found — run `python -m script_report build` first.")
        sys.exit(1)
    text = SITE_DATA_JS.read_text(encoding="utf-8")
    m = re.search(r"window\.SITE_DATA\s*=\s*(\{.*\});", text, re.S)
    if not m:
        print("ERROR: could not parse window.SITE_DATA assignment from site_data.js")
        sys.exit(1)
    return json.loads(m.group(1))


def _clean_dir(path: Path) -> int:
    """Wipe a directory recursively. Returns count of files removed."""
    if not path.exists():
        return 0
    n = sum(1 for _ in path.rglob("*") if _.is_file())
    shutil.rmtree(path)
    return n


def generate_static_pages(site_data: dict, out_root: Path, limit: int = 0) -> dict:
    """Write all static pages + sitemap + robots. Returns counts/sizes."""
    psd = site_data.get("psd") or {}
    drugs = psd.get("drugs") or {}
    sponsor_details = psd.get("sponsor_details") or {}
    drug_spend_by = ((site_data.get("drug_spend") or {}).get("by_drug")) or {}

    out_drugs = out_root / "drug"
    out_sponsors = out_root / "sponsor"
    out_drugs.mkdir(parents=True, exist_ok=True)
    out_sponsors.mkdir(parents=True, exist_ok=True)

    drug_urls: list[str] = []
    drug_bytes = 0
    drug_collisions: list[tuple[str, str, str]] = []   # (slug, kept_key, dropped_key)
    seen_drug_slugs: dict[str, str] = {}
    drug_items = list(drugs.items())
    if limit:
        drug_items = drug_items[:limit]
    for key, drug in drug_items:
        slug = slugify(key)
        if not slug:
            continue
        if slug in seen_drug_slugs:
            drug_collisions.append((slug, seen_drug_slugs[slug], key))
            continue
        seen_drug_slugs[slug] = key
        page_dir = out_drugs / slug
        page_dir.mkdir(parents=True, exist_ok=True)
        html_str = _render_drug_page(key, drug, drug_spend_by)
        (page_dir / "index.html").write_text(html_str, encoding="utf-8")
        drug_bytes += len(html_str.encode("utf-8"))
        drug_urls.append(f"{SITE_URL}/drug/{slug}/")

    sponsor_urls: list[str] = []
    sponsor_bytes = 0
    seen_sponsor_slugs: dict[str, str] = {}
    sponsor_items = list(sponsor_details.items())
    if limit:
        sponsor_items = sponsor_items[:limit]
    for key, det in sponsor_items:
        slug = slugify(key)
        if not slug or det.get("n_submissions", 0) < 1:
            continue
        if slug in seen_sponsor_slugs:
            continue
        seen_sponsor_slugs[slug] = key
        page_dir = out_sponsors / slug
        page_dir.mkdir(parents=True, exist_ok=True)
        html_str = _render_sponsor_page(key, det)
        (page_dir / "index.html").write_text(html_str, encoding="utf-8")
        sponsor_bytes += len(html_str.encode("utf-8"))
        sponsor_urls.append(f"{SITE_URL}/sponsor/{slug}/")

    lastmod = date.today().isoformat()
    (out_root / "sitemap.xml").write_text(
        _render_sitemap(drug_urls, sponsor_urls, lastmod), encoding="utf-8"
    )
    (out_root / "robots.txt").write_text(_render_robots(), encoding="utf-8")

    return {
        "drug_pages":      len(drug_urls),
        "sponsor_pages":   len(sponsor_urls),
        "drug_kb":         drug_bytes // 1024,
        "sponsor_kb":      sponsor_bytes // 1024,
        "avg_drug_kb":     (drug_bytes // max(len(drug_urls), 1)) / 1024,
        "avg_sponsor_kb":  (sponsor_bytes // max(len(sponsor_urls), 1)) / 1024,
        "drug_collisions": drug_collisions,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Pre-render static SEO landing pages for drug + sponsor detail.")
    parser.add_argument("--clean", action="store_true", help="Wipe drug/ and sponsor/ dirs first (removes stale slugs)")
    parser.add_argument("--limit", type=int, default=0, help="Process only first N drugs and sponsors (smoke test)")
    args = parser.parse_args()

    if args.clean:
        n1 = _clean_dir(REPO_ROOT / "drug")
        n2 = _clean_dir(REPO_ROOT / "sponsor")
        if n1 or n2:
            print(f"  Cleaned: {n1} drug files, {n2} sponsor files")

    print("=" * 60)
    print("Pre-rendering static SEO pages")
    print("=" * 60)
    site_data = _load_site_data()
    print(f"  Loaded site_data.js  ({SITE_DATA_JS.stat().st_size // 1024} KB)")

    stats = generate_static_pages(site_data, REPO_ROOT, limit=args.limit)
    print(f"  Drug pages    : {stats['drug_pages']:>4}  ({stats['drug_kb']:>5,} KB total, avg {stats['avg_drug_kb']:.1f} KB)")
    print(f"  Sponsor pages : {stats['sponsor_pages']:>4}  ({stats['sponsor_kb']:>5,} KB total, avg {stats['avg_sponsor_kb']:.1f} KB)")
    print(f"  sitemap.xml   : {(REPO_ROOT / 'sitemap.xml').stat().st_size // 1024} KB")
    print(f"  robots.txt    : {(REPO_ROOT / 'robots.txt').stat().st_size} bytes")
    collisions = stats.get("drug_collisions") or []
    if collisions:
        print(f"  Slug collisions skipped ({len(collisions)}): variant drug keys that map to the same slug")
        for slug, kept, dropped in collisions[:6]:
            print(f"    /{slug}/  kept '{kept}'  dropped '{dropped}'")
        if len(collisions) > 6:
            print(f"    ... and {len(collisions) - 6} more")
    print()
    print("Next steps:")
    print("  - git add drug/ sponsor/ sitemap.xml robots.txt")
    print("  - commit + push")
    print("  - submit sitemap to Google Search Console")


if __name__ == "__main__":
    main()
