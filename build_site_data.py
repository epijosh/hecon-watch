"""
build_site_data.py
━━━━━━━━━━━━━━━━━━
Reads all available data sources and writes site_data.js —
a single JS file that embeds every dataset the site needs.

DATA SOURCES (any that exist are incorporated):
  atc_benefit.csv      — PBS government benefit by ATC class, year, state
  atc_services.csv     — PBS prescriptions by ATC class, year
  psd_extracted.csv    — Structured PSD fields (from extract_psd_text.py)
  pbs_drug_spend.csv   — Drug-level spend (from fetch_pbs_drug_spend.py)
  *.pdf (PSD names)    — PBAC Public Summary Documents (parsed from filenames)

OUTPUT:
  site_data.js         — window.SITE_DATA = { ... }  (loads before site_preview.html)

USAGE:
  python build_site_data.py

Then open site_preview.html — it auto-loads site_data.js if present.
"""

from __future__ import annotations
import csv
import json
import re
from pathlib import Path
from datetime import datetime

HERE = Path(__file__).parent
DATA = HERE / "data"          # CSV/data outputs go here; fallback to HERE for legacy files
DATA.mkdir(exist_ok=True)

def _d(filename: str) -> Path:
    """Resolve a data file path: prefer data/ subfolder, fall back to root."""
    p = DATA / filename
    return p if p.exists() else HERE / filename

MONTH_MAP = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# ── 1. PBS ATC Data ───────────────────────────────────────────────────────────

def load_atc_data() -> dict:
    """
    Load atc_benefit.csv and atc_services.csv.

    Returns:
      {
        "grand_total_by_year": {year: benefit_aud},      # PBS total all classes
        "classes_by_year":     {class: {year: benefit}}, # individual ATC classes
        "atc_meta":            {class: {code, total_2024, ...}},
        "services_by_year":    {year: total_services},
      }
    """
    result = {
        "grand_total_by_year": {},
        "classes_by_year": {},
        "atc_meta": {},
        "services_by_year": {},
    }

    # ── Benefit data ──────────────────────────────────────────────────────────
    benefit_path = _d("atc_benefit.csv")
    if not benefit_path.exists():
        print("  atc_benefit.csv not found — run parse_atc_data.py first")
        return result

    rows = []
    with open(benefit_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # ── Build per-ATC-class series, then SUM them for grand total ────────────
    # Each ATC class may have multiple rows per year (different magnitude brackets
    # from the data source). Strategy:
    #   1. For each ATC class, collect all (year, value) rows
    #   2. Per year, keep the MEDIAN value to exclude outliers
    #   3. Sum across all classes per year → grand total
    # This is robust: no hard thresholds, no spurious "total" row detection.

    individual_classes: dict[str, dict[int, int]] = {}  # atc → {year → value}
    raw_by_atc_year: dict[str, dict[int, list[int]]] = {}  # atc → year → [vals]

    for row in rows:
        year_s = row.get("year", "")
        if year_s in ("TOTAL",) or "_ytd" in year_s:
            continue
        try:
            year = int(year_s)
        except ValueError:
            continue
        if year < 1992 or year > 2030:
            continue
        atc = row.get("atc", "").strip()
        if not atc:
            continue
        total = row.get("total", "0")
        try:
            val = int(float(total))
        except (ValueError, TypeError):
            continue
        if val <= 0:
            continue

        raw_by_atc_year.setdefault(atc, {}).setdefault(year, []).append(val)

    # For each ATC class+year pick the MINIMUM (most conservative single-class value)
    # Multiple values per year arise when the PBS XLS has sub-group rows; the minimum
    # is almost always the correct single-class figure.
    for atc, year_list in raw_by_atc_year.items():
        by_year: dict[int, int] = {}
        for yr, vals in year_list.items():
            by_year[yr] = min(vals)   # take minimum to avoid summing sub-rows
        individual_classes[atc] = by_year

    # Grand total = sum of all individual class values per year
    grand_total: dict[int, int] = {}
    for by_year in individual_classes.values():
        for yr, val in by_year.items():
            grand_total[yr] = grand_total.get(yr, 0) + val

    # Convert to sorted string-keyed dicts for JSON
    result["grand_total_by_year"] = {str(y): v for y, v in sorted(grand_total.items())}

    for atc_name, by_year in individual_classes.items():
        result["classes_by_year"][atc_name] = {str(y): v for y, v in sorted(by_year.items())}

    # ATC meta: which classes are present and their 2024 spend
    for atc_name, by_year in individual_classes.items():
        val_2024 = by_year.get(2024, 0)
        result["atc_meta"][atc_name] = {"spend_2024_aud": val_2024}

    print(f"  ATC benefit: {len(individual_classes)} individual classes, grand total {len(grand_total)} years")

    # ── Services data ─────────────────────────────────────────────────────────
    services_path = _d("atc_services.csv")
    if services_path.exists():
        with open(services_path, encoding="utf-8") as f:
            srows = list(csv.DictReader(f))
        svc_grand: dict[int, int] = {}
        for row in srows:
            year_s = row.get("year", "")
            if year_s in ("TOTAL",) or "_ytd" in year_s:
                continue
            try:
                year = int(year_s)
            except ValueError:
                continue
            try:
                val = int(float(row.get("total", 0)))
            except (ValueError, TypeError):
                continue
            if val > 50_000_000:  # Grand total threshold for services (>50M scripts/year)
                if year not in svc_grand or val > svc_grand[year]:
                    svc_grand[year] = val
        result["services_by_year"] = {str(y): v for y, v in sorted(svc_grand.items())}
        print(f"  ATC services: grand total {len(svc_grand)} years")

    return result


# ── 2. PBAC PSD Data ──────────────────────────────────────────────────────────

def load_pbac_psds() -> dict:
    """
    Scan PSD PDF filenames to extract drug names and meeting dates.
    Returns counts by year and therapy area.
    """
    PSD_PATTERN = re.compile(
        r'^(.+?)-psd-([a-z]+)-(\d{4})\.pdf$', re.IGNORECASE
    )

    psds = []
    seen_files = set()
    # Scan PSDs in both the project root AND the data/psds/ subfolder
    scan_dirs = [HERE, DATA / "psds", DATA]
    for d in scan_dirs:
        if not d.exists():
            continue
        for pdf in d.glob("*.pdf"):
            if pdf.name in seen_files:
                continue
            m = PSD_PATTERN.match(pdf.name.lower())
            if not m:
                continue
            drug, month_s, year_s = m.group(1), m.group(2), m.group(3)
            month = MONTH_MAP.get(month_s[:3])
            if not month:
                continue
            seen_files.add(pdf.name)
            psds.append({
                "drug":  drug.replace("-", " ").title(),
                "month": month,
                "year":  int(year_s),
                "filename": pdf.name,
            })

    by_year: dict[str, int] = {}
    for p in psds:
        yr = str(p["year"])
        by_year[yr] = by_year.get(yr, 0) + 1

    print(f"  PBAC PSDs: {len(psds)} documents, {len(by_year)} years")
    return {
        "total": len(psds),
        "by_year": dict(sorted(by_year.items())),
        "drugs": sorted(set(p["drug"] for p in psds)),
        "year_range": [min(p["year"] for p in psds), max(p["year"] for p in psds)] if psds else [0, 0],
    }


# ── 3. PSD Extracted Data ─────────────────────────────────────────────────────

def load_psd_extracted() -> dict:
    """
    Load psd_extracted.csv (from extract_psd_text.py).
    Groups by drug name, produces per-drug summaries with submission history.
    """
    path = _d("psd_extracted.csv")
    if not path.exists():
        print("  psd_extracted.csv not found — run extract_psd_text.py first")
        return {"total": 0, "drugs": {}, "by_recommendation": {}, "by_therapy": {}}

    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    good = [r for r in rows if r.get("extraction_ok") == "yes"]
    print(f"  PSD extracted: {len(good)} successful rows from {len(rows)} total")

    # Group by drug name
    by_drug: dict[str, list[dict]] = {}
    for row in good:
        drug = (row.get("drug") or "").strip().lower()
        if not drug:
            m = re.match(r'^(.+?)-psd-', (row.get("filename") or "").lower())
            drug = m.group(1).replace("-", " ") if m else ""
        if not drug:
            continue
        by_drug.setdefault(drug, []).append(row)

    def _int(val):
        try:
            return int(float(val)) if val and str(val).strip() not in ("", "None") else None
        except (ValueError, TypeError):
            return None

    def _sort_key(r):
        yr = _int(r.get("pbac_year")) or 0
        mo = _int(r.get("pbac_month")) or 0
        return yr * 100 + mo

    drug_summaries: dict[str, dict] = {}
    for drug, decisions in by_drug.items():
        decisions.sort(key=_sort_key)
        latest = decisions[-1]

        icer_lows  = [_int(r.get("icer_low"))  for r in decisions if _int(r.get("icer_low"))]
        icer_highs = [_int(r.get("icer_high")) for r in decisions if _int(r.get("icer_high"))]

        history = []
        for r in decisions[-6:]:   # last 6 submissions
            history.append({
                "year":        r.get("pbac_year", ""),
                "month":       r.get("pbac_month", ""),
                "rec":         r.get("recommendation", ""),
                "icer_low":    _int(r.get("icer_low")),
                "icer_high":   _int(r.get("icer_high")),
                "listing_type": r.get("listing_type", ""),
                "filename":    r.get("filename", ""),
            })

        drug_summaries[drug] = {
            "drug":             drug,
            "brand_name":       latest.get("brand_name", ""),
            "indication":       latest.get("indication", ""),
            "therapy_area":     latest.get("therapy_area", ""),
            "recommendation":   latest.get("recommendation", ""),
            "listing_type":     latest.get("listing_type", ""),
            "comparator":       latest.get("comparator", ""),
            "icer_low":         min(icer_lows)  if icer_lows  else None,
            "icer_high":        max(icer_highs) if icer_highs else None,
            "icer_note":        latest.get("icer_note", ""),
            "risk_sharing":     latest.get("risk_sharing", "") == "yes",
            "risk_sharing_note": latest.get("risk_sharing_note", ""),
            "population_per_year": _int(latest.get("population_per_year")),
            "key_trials":       latest.get("key_trials", ""),
            "submissions":      len(decisions),
            "resubmissions":    sum(1 for r in decisions if r.get("resubmission") == "yes"),
            "first_year":       decisions[0].get("pbac_year", ""),
            "latest_year":      latest.get("pbac_year", ""),
            "history":          history,
            # ── Deeper fields (May 2026) ──────────────────────────────────────
            "budget_impact_aud":  _int(latest.get("budget_impact_aud")),
            "rejection_reasons":  latest.get("rejection_reasons", "") or None,
            "patient_advocacy":   latest.get("patient_advocacy", "") == "yes",
            "pico_population":    latest.get("pico_population", "") or None,
            "evidence_type":      latest.get("evidence_type", "") or None,
            "line_of_therapy":    latest.get("line_of_therapy", "") or None,
            "trial_size":         _int(latest.get("trial_size")),
            "primary_endpoint":   latest.get("primary_endpoint", "") or None,
            "economic_model":     latest.get("economic_model", "") or None,
        }

    by_rec: dict[str, int] = {}
    by_therapy: dict[str, int] = {}
    for d in drug_summaries.values():
        r = d["recommendation"]
        if r: by_rec[r] = by_rec.get(r, 0) + 1
        t = d["therapy_area"]
        if t: by_therapy[t] = by_therapy.get(t, 0) + 1

    # Year-by-year volume of every extracted PSD record (not just one per drug)
    by_year: dict[str, int] = {}
    for row in good:
        yr = (row.get("pbac_year") or "").strip()
        if yr.isdigit() and 2000 <= int(yr) <= 2030:
            by_year[yr] = by_year.get(yr, 0) + 1

    total_recommended = by_rec.get("Recommended", 0) + by_rec.get("Recommended with restriction", 0)
    total_with_rec    = sum(by_rec.values())
    print(f"  Unique drugs: {len(drug_summaries)}  |  "
          f"Recommended: {total_recommended}  |  "
          f"Not recommended: {by_rec.get('Not recommended', 0)}")
    print(f"  Year coverage: {len(by_year)} years, {sum(by_year.values())} records")

    return {
        "total":            len(drug_summaries),
        "drugs":            drug_summaries,
        "by_recommendation": dict(sorted(by_rec.items(),    key=lambda x: -x[1])),
        "by_therapy":        dict(sorted(by_therapy.items(), key=lambda x: -x[1])),
        "by_year":           dict(sorted(by_year.items())),
    }


# ── 4. Nearest-neighbour links from embed_psds.py ─────────────────────────────

def load_psd_nearest() -> dict:
    """Load data/psd_nearest.json (produced by embed_psds.py).

    Schema:
        { drug_name: [ {drug: <other_name>, score: <float>}, ... ] }
    """
    path = _d("psd_nearest.json")
    if not path.exists():
        print("  psd_nearest.json not found — run embed_psds.py for 'Similar drugs' links")
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        # Normalise keys to lowercase to match psd['drugs'] keying
        out = {}
        for k, v in data.items():
            key = (k or "").strip().lower()
            if not key or not isinstance(v, list):
                continue
            cleaned = []
            for item in v:
                if not isinstance(item, dict):
                    continue
                name = (item.get("drug") or "").strip().lower()
                score = item.get("score")
                if name and isinstance(score, (int, float)):
                    cleaned.append({"drug": name, "score": float(round(score, 4))})
            if cleaned:
                out[key] = cleaned
        print(f"  Nearest-neighbour table: {len(out)} drugs")
        return out
    except Exception as e:
        print(f"  ⚠ Could not parse psd_nearest.json ({e})")
        return {}


def attach_nearest_to_psd(psd: dict, nearest: dict) -> None:
    """Fold the nearest-neighbour list onto each drug record in psd['drugs']."""
    if not psd or not psd.get("drugs") or not nearest:
        return
    drugs = psd["drugs"]
    matched = 0
    for key, drug_summary in drugs.items():
        nbrs = nearest.get(key.lower())
        if not nbrs:
            continue
        # Filter out neighbours we don't have in the local set
        valid = [n for n in nbrs if n["drug"] in drugs]
        if not valid:
            continue
        # Limit to 10 — the dashboard shows at most a handful, and this keeps the JS file small
        drug_summary["nearest"] = valid[:10]
        matched += 1
    if matched:
        print(f"  Attached nearest-neighbour lists to {matched} drugs")


# ── 7. Drug-level PBS spend ───────────────────────────────────────────────────

def load_drug_spend() -> dict:
    """
    Load pbs_drug_spend.csv (from fetch_pbs_drug_spend.py).
    Returns top drugs by spend and by cost-per-script.
    """
    path = _d("pbs_drug_spend.csv")
    if not path.exists():
        print("  pbs_drug_spend.csv not found — run fetch_pbs_drug_spend.py first")
        return {"total": 0, "top_spend": [], "top_cost_per_script": [], "by_drug": {}}

    def _int(v):
        try:
            return int(float(v)) if v and str(v).strip() not in ("", "None") else None
        except (ValueError, TypeError):
            return None

    def _float(v):
        try:
            return float(v) if v and str(v).strip() not in ("", "None") else None
        except (ValueError, TypeError):
            return None

    _KEEP_UPPER = {"IV", "PBS", "ATC", "DNA", "RNA", "PEG", "HPV", "BCG", "MMR"}

    def _title(name: str) -> str:
        if not name or not name.isupper():
            return name
        return " ".join(w if w in _KEEP_UPPER else w.capitalize() for w in name.split())

    raw_rows = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            benefit = _int(row.get("gov_benefit_aud"))
            scripts = _int(row.get("scripts"))
            raw_name = re.sub(r'[\^*#]+$', '', (row.get("drug_name") or "").strip()).strip()
            drug    = _title(raw_name)
            if not drug or benefit is None:
                continue
            raw_rows.append({
                "drug_name":   drug,
                "brand_name":  (row.get("brand_name") or "").strip(),
                "atc_code":    (row.get("atc_code") or "").strip(),
                "gov_benefit_aud": benefit,
                "scripts":     scripts or 0,
                "_best_b":     benefit,
                "report_year": row.get("report_year", ""),
            })

    # Deduplicate — aggregate same drug across formulations
    agg: dict = {}
    for r in raw_rows:
        key = r["drug_name"].lower()
        if key not in agg:
            agg[key] = r.copy()
        else:
            agg[key]["gov_benefit_aud"] += r["gov_benefit_aud"]
            agg[key]["scripts"] = (agg[key]["scripts"] or 0) + (r["scripts"] or 0)
            if r["gov_benefit_aud"] > agg[key]["_best_b"]:
                agg[key]["_best_b"]    = r["gov_benefit_aud"]
                agg[key]["brand_name"] = r["brand_name"]
                agg[key]["atc_code"]   = r["atc_code"]

    rows = []
    for entry in agg.values():
        b = entry["gov_benefit_aud"]
        s = entry["scripts"]
        cps = round(b / s, 2) if s and s > 0 else None
        rows.append({
            "drug_name":           entry["drug_name"],
            "brand_name":          entry["brand_name"],
            "atc_code":            entry["atc_code"],
            "gov_benefit_aud":     b,
            "scripts":             s if s else None,
            "cost_per_script_aud": cps,
            "report_year":         entry["report_year"],
        })

    if not rows:
        print("  pbs_drug_spend.csv loaded but no rows parsed")
        return {"total": 0, "top_spend": [], "top_cost_per_script": [], "by_drug": {}}

    year = rows[0].get("report_year", "2024") if rows else "2024"

    # Top 20 by government benefit
    top_spend = sorted(rows, key=lambda r: r["gov_benefit_aud"], reverse=True)[:20]

    # Top 20 by cost per script (minimum 50 scripts so it's not noise)
    top_cps = sorted(
        [r for r in rows if r.get("cost_per_script_aud") and (r.get("scripts") or 0) >= 50],
        key=lambda r: r["cost_per_script_aud"],
        reverse=True,
    )[:20]

    # Lookup dict by normalised drug name
    by_drug = {r["drug_name"].lower(): r for r in rows}

    total_benefit = sum(r["gov_benefit_aud"] for r in rows)
    print(f"  Drug spend: {len(rows)} drugs, total govt benefit ${total_benefit/1e9:.2f}B ({year})")
    print(f"    Top drug: {top_spend[0]['drug_name']} (${top_spend[0]['gov_benefit_aud']/1e6:.0f}M)" if top_spend else "")
    print(f"    Highest cost/script: {top_cps[0]['drug_name']} (${top_cps[0]['cost_per_script_aud']:,.0f})" if top_cps else "")

    return {
        "total":              len(rows),
        "report_year":        year,
        "top_spend":          top_spend,
        "top_cost_per_script": top_cps,
        "by_drug":            by_drug,
    }


# ── 7. Write site_data.js ─────────────────────────────────────────────────────

def write_site_data(pbs: dict, pbac: dict, psd: dict,
                    drug_spend: dict | None = None):
    now = datetime.now().strftime("%Y-%m-%d")

    # Compute key stats for the stats bar
    pbs_2024   = pbs["grand_total_by_year"].get("2024", 0)
    _pbs_sorted = sorted(pbs["grand_total_by_year"].keys())
    _earliest_val = pbs["grand_total_by_year"].get(_pbs_sorted[0], 1) if _pbs_sorted else 1
    pbs_growth = round(pbs_2024 / _earliest_val, 1) if _earliest_val else 0

    scripts_2024 = pbs["services_by_year"].get("2024", 0)

    site_data = {
        "generated": now,
        "stats": {
            "pbac_psds":         pbac["total"],
            "pbac_years":        pbac["year_range"],
            "pbs_2024_aud":      pbs_2024,
            "pbs_growth_x":      pbs_growth,
            "scripts_2024":      scripts_2024,
        },
        "pbs": {
            "grand_total_by_year": pbs["grand_total_by_year"],
            "services_by_year":    pbs["services_by_year"],
            "classes_by_year":     pbs["classes_by_year"],
            "atc_meta":            pbs["atc_meta"],
        },
        "pbac":       pbac,
        "psd":        psd,
        "drug_spend": drug_spend or {"total": 0, "top_spend": [], "top_cost_per_script": [], "by_drug": {}},
    }

    js = f"""// site_data.js — auto-generated by build_site_data.py on {now}
// Do not edit manually — run: python build_site_data.py
window.SITE_DATA = {json.dumps(site_data, ensure_ascii=False, separators=(',', ':'))};
"""

    out = HERE / "site_data.js"
    out.write_text(js, encoding="utf-8")
    size_kb = out.stat().st_size // 1024
    print(f"  Written: site_data.js  ({size_kb} KB)")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("Building site_data.js")
    print("=" * 60)

    print("\n[1/5] Loading PBS ATC data...")
    pbs = load_atc_data()

    print("\n[2/5] Scanning PBAC PSDs...")
    pbac = load_pbac_psds()

    print("\n[3/5] Loading extracted PSD data...")
    psd = load_psd_extracted()

    print("\n[4/5] Loading PBS drug-level spend...")
    drug_spend = load_drug_spend()

    print("\n[5/5] Loading nearest-neighbour links (embed_psds.py output)...")
    nearest = load_psd_nearest()
    attach_nearest_to_psd(psd, nearest)

    print("\nWriting output...")
    write_site_data(pbs, pbac, psd, drug_spend)

    # Summary
    gt = pbs["grand_total_by_year"]
    print("\n" + "=" * 60)
    print("KEY STATS")
    print("=" * 60)
    _pbs_years = sorted(pbs['grand_total_by_year'].keys())
    _pbs_first = _pbs_years[0] if _pbs_years else 'n/a'
    _pbs_last  = _pbs_years[-1] if _pbs_years else 'n/a'
    _v_first   = pbs['grand_total_by_year'].get(_pbs_first, 1) or 1
    _v_last    = pbs['grand_total_by_year'].get(_pbs_last, 0)
    print(f"  PBS total {_pbs_first}  : ${_v_first/1e9:.2f}B")
    print(f"  PBS total {_pbs_last}  : ${_v_last/1e9:.2f}B")
    print(f"  Growth             : {_v_last/_v_first:.1f}x")
    print(f"  Scripts 2024       : {pbs['services_by_year'].get('2024',0)/1e6:.1f}M")
    print(f"  PBAC PSDs          : {pbac['total']}")
    print(f"  Extracted drugs    : {psd['total']}")
    rec_counts = psd.get('by_recommendation', {})
    for rec, n in rec_counts.items():
        print(f"    {rec:<35} {n}")
    print()
    print("  ATC classes in benefit data:")
    for atc, meta in sorted(pbs["atc_meta"].items(), key=lambda x: -x[1].get("spend_2024_aud",0)):
        spend = meta["spend_2024_aud"]
        if spend > 0:
            print(f"    {atc[:50]:<50} ${spend/1e9:.2f}B (2024)")
    print()
    print("Done. Open site_preview.html to see the updated site.")


if __name__ == "__main__":
    main()
