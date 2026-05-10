"""Read CSV/JSON inputs from ``data/`` into shape for the site builder.

Each loader returns a plain ``dict`` ready to drop into ``site_data.js`` (or to
be used as input to a downstream processor). They print compact status lines
to stdout while running so a long pipeline run is legible in a terminal.

Inputs are tolerant: a missing file logs a hint and returns an empty payload
rather than raising — that lets ``build`` succeed even when only a subset of
the pipeline has been run.
"""

from __future__ import annotations

import csv
import json
import re

from script_report.config import DATA_DIR, REPO_ROOT
from script_report.utils.helpers import MONTH_MAP, data_path


# ── 1. PBS ATC class spend / scripts ─────────────────────────────────────────

def load_atc_data() -> dict:
    """Load atc_benefit.csv and atc_services.csv.

    Returns:
      {
        "grand_total_by_year": {year: benefit_aud},
        "classes_by_year":     {class: {year: benefit}},
        "atc_meta":            {class: {spend_2024_aud}},
        "services_by_year":    {year: total_services},
      }
    """
    result = {
        "grand_total_by_year": {},
        "classes_by_year": {},
        "atc_meta": {},
        "services_by_year": {},
    }

    # ── Benefit data ─────────────────────────────────────────────────────────
    benefit_path = data_path("atc_benefit.csv")
    if not benefit_path.exists():
        print("  atc_benefit.csv not found — run parse_atc_data.py first")
        return result

    with open(benefit_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # Build per-ATC-class series, then SUM them for grand total.
    # Each ATC class may have multiple rows per year (different magnitude
    # brackets from the data source). Strategy:
    #   1. For each ATC class, collect all (year, value) rows.
    #   2. Per year, take the MINIMUM value (the conservative single-class
    #      figure; the larger values are sub-group sums we don't want to
    #      double-count).
    #   3. Sum across all classes per year → grand total.
    individual_classes: dict[str, dict[int, int]] = {}
    raw_by_atc_year: dict[str, dict[int, list[int]]] = {}

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
        try:
            val = int(float(row.get("total", "0")))
        except (ValueError, TypeError):
            continue
        if val <= 0:
            continue

        raw_by_atc_year.setdefault(atc, {}).setdefault(year, []).append(val)

    for atc, year_list in raw_by_atc_year.items():
        individual_classes[atc] = {yr: min(vals) for yr, vals in year_list.items()}

    grand_total: dict[int, int] = {}
    for by_year in individual_classes.values():
        for yr, val in by_year.items():
            grand_total[yr] = grand_total.get(yr, 0) + val

    result["grand_total_by_year"] = {str(y): v for y, v in sorted(grand_total.items())}
    for atc_name, by_year in individual_classes.items():
        result["classes_by_year"][atc_name] = {str(y): v for y, v in sorted(by_year.items())}
    for atc_name, by_year in individual_classes.items():
        result["atc_meta"][atc_name] = {"spend_2024_aud": by_year.get(2024, 0)}

    print(f"  ATC benefit: {len(individual_classes)} individual classes, grand total {len(grand_total)} years")

    # ── Services data ────────────────────────────────────────────────────────
    services_path = data_path("atc_services.csv")
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
            # Grand-total threshold for services: >50M scripts/year
            if val > 50_000_000 and (year not in svc_grand or val > svc_grand[year]):
                svc_grand[year] = val
        result["services_by_year"] = {str(y): v for y, v in sorted(svc_grand.items())}
        print(f"  ATC services: grand total {len(svc_grand)} years")

    return result


# ── 2. PBAC PSDs (filename scan) ─────────────────────────────────────────────

def load_pbac_psds() -> dict:
    """Scan PSD PDF filenames to extract drug names + meeting dates.

    Returns total counts by year + a sorted drug list + the year range.
    """
    PSD_PATTERN = re.compile(r'^(.+?)-psd-([a-z]+)-(\d{4})\.pdf$', re.IGNORECASE)

    psds = []
    seen_files: set[str] = set()
    # Scan PSDs in both the project root AND the data/psds/ subfolder
    scan_dirs = [REPO_ROOT, DATA_DIR / "psds", DATA_DIR]
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
                "drug":     drug.replace("-", " ").title(),
                "month":    month,
                "year":     int(year_s),
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


# ── 3. PSD extracted CSV ─────────────────────────────────────────────────────

def _safe_int(val) -> int | None:
    try:
        return int(float(val)) if val and str(val).strip() not in ("", "None") else None
    except (ValueError, TypeError):
        return None


def _outcome_bucket(rec: str) -> str:
    """Bucket a PBAC recommendation string into rec / not / deferred / unknown.
    Mirrors the JS recBucket() helper so Python and JS agree on the taxonomy.
    """
    s = (rec or "").lower()
    if s.startswith("recommended") and not s.startswith("not"):
        return "rec"
    if s.startswith("not"):
        return "not"
    if s == "deferred":
        return "deferred"
    return "unknown"


def _compute_deltas(history: list[dict]) -> list[dict]:
    """Given a chronologically sorted history, compute per-pair deltas.

    Each delta describes what changed between PSD N and PSD N+1: outcome flips,
    ICER moves (with %change), comparator switches, evidence-type upgrades,
    listing-type changes. Returns a list of one dict per pair (so a drug with
    4 PSDs produces 3 deltas).
    """
    out: list[dict] = []
    for i in range(1, len(history)):
        prev, cur = history[i - 1], history[i]
        changes: list[str] = []

        prev_b, cur_b = _outcome_bucket(prev.get("rec")), _outcome_bucket(cur.get("rec"))
        outcome_flip = (prev_b != cur_b) and (prev_b in ("rec", "not", "deferred")) and (cur_b in ("rec", "not", "deferred"))

        # ICER move
        prev_icer = prev.get("icer_high") or prev.get("icer_low")
        cur_icer  = cur.get("icer_high")  or cur.get("icer_low")
        icer_change_aud = None
        icer_change_pct = None
        if prev_icer and cur_icer and prev_icer > 0:
            delta = cur_icer - prev_icer
            icer_change_aud = delta
            icer_change_pct = round(delta / prev_icer * 100, 1)
            if abs(icer_change_pct) >= 5:
                direction = "dropped" if delta < 0 else "rose"
                changes.append(f"ICER {direction} ${prev_icer:,} → ${cur_icer:,} ({icer_change_pct:+.0f}%)")

        # Comparator pivot
        prev_comp = (prev.get("comparator") or "").strip()
        cur_comp  = (cur.get("comparator") or "").strip()
        if prev_comp and cur_comp and prev_comp.lower() != cur_comp.lower():
            changes.append(f"Comparator changed: {prev_comp} → {cur_comp}")

        # Evidence upgrade (Single-arm → RCT etc.)
        prev_ev = (prev.get("evidence_type") or "").strip()
        cur_ev  = (cur.get("evidence_type") or "").strip()
        if prev_ev and cur_ev and prev_ev != cur_ev:
            changes.append(f"Evidence: {prev_ev} → {cur_ev}")

        # Listing type
        prev_lt = (prev.get("listing_type") or "").strip()
        cur_lt  = (cur.get("listing_type") or "").strip()
        if prev_lt and cur_lt and prev_lt != cur_lt:
            changes.append(f"Listing: {prev_lt} → {cur_lt}")

        out.append({
            "from_year":     prev.get("year", ""),
            "from_month":    prev.get("month", ""),
            "to_year":       cur.get("year", ""),
            "to_month":      cur.get("month", ""),
            "from_rec":      prev.get("rec", ""),
            "to_rec":        cur.get("rec", ""),
            "outcome_flip":  outcome_flip,
            "from_bucket":   prev_b,
            "to_bucket":     cur_b,
            "icer_change":   icer_change_aud,
            "icer_pct":      icer_change_pct,
            "changes":       changes,
            "prior_concerns": (prev.get("rejection_reasons") or "").strip()[:160] or None,
        })
    return out


def load_psd_extracted() -> dict:
    """Load psd_extracted.csv (from extract_psd_text.py).

    Groups by drug name; latest decision contributes the displayed fields,
    last 6 decisions feed the submission-history timeline.
    """
    path = data_path("psd_extracted.csv")
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

    def _sort_key(r):
        yr = _safe_int(r.get("pbac_year")) or 0
        mo = _safe_int(r.get("pbac_month")) or 0
        return yr * 100 + mo

    drug_summaries: dict[str, dict] = {}
    for drug, decisions in by_drug.items():
        decisions.sort(key=_sort_key)
        latest = decisions[-1]

        icer_lows  = [_safe_int(r.get("icer_low"))  for r in decisions if _safe_int(r.get("icer_low"))]
        icer_highs = [_safe_int(r.get("icer_high")) for r in decisions if _safe_int(r.get("icer_high"))]

        history = [
            {
                "year":             r.get("pbac_year", ""),
                "month":            r.get("pbac_month", ""),
                "rec":              r.get("recommendation", ""),
                "icer_low":         _safe_int(r.get("icer_low")),
                "icer_high":        _safe_int(r.get("icer_high")),
                "listing_type":     r.get("listing_type", ""),
                "filename":         r.get("filename", ""),
                # Extra fields used for delta computation between consecutive PSDs
                "comparator":       (r.get("comparator") or "").strip()[:120],
                "evidence_type":    r.get("evidence_type", ""),
                "primary_endpoint": r.get("primary_endpoint", ""),
                "rejection_reasons": (r.get("rejection_reasons") or "").strip()[:200],
            }
            for r in decisions[-6:]
        ]
        deltas = _compute_deltas(history)

        drug_summaries[drug] = {
            "drug":              drug,
            "brand_name":        latest.get("brand_name", ""),
            "indication":        latest.get("indication", ""),
            "therapy_area":      latest.get("therapy_area", ""),
            "recommendation":    latest.get("recommendation", ""),
            "listing_type":      latest.get("listing_type", ""),
            "comparator":        latest.get("comparator", ""),
            "icer_low":          min(icer_lows)  if icer_lows  else None,
            "icer_high":         max(icer_highs) if icer_highs else None,
            "icer_note":         latest.get("icer_note", ""),
            "risk_sharing":      latest.get("risk_sharing", "") == "yes",
            "risk_sharing_note": latest.get("risk_sharing_note", ""),
            "population_per_year": _safe_int(latest.get("population_per_year")),
            "key_trials":        latest.get("key_trials", ""),
            "submissions":       len(decisions),
            "resubmissions":     sum(1 for r in decisions if r.get("resubmission") == "yes"),
            "first_year":        decisions[0].get("pbac_year", ""),
            "latest_year":       latest.get("pbac_year", ""),
            "history":           history,
            "deltas":            deltas,
            # ── Deeper fields (May 2026) ──────────────────────────────────────
            "budget_impact_aud": _safe_int(latest.get("budget_impact_aud")),
            "rejection_reasons": latest.get("rejection_reasons", "") or None,
            "patient_advocacy":  latest.get("patient_advocacy", "") == "yes",
            "pico_population":   latest.get("pico_population", "") or None,
            "evidence_type":     latest.get("evidence_type", "") or None,
            "line_of_therapy":   latest.get("line_of_therapy", "") or None,
            "trial_size":        _safe_int(latest.get("trial_size")),
            "primary_endpoint":  latest.get("primary_endpoint", "") or None,
            "economic_model":    latest.get("economic_model", "") or None,
        }

    by_rec: dict[str, int] = {}
    by_therapy: dict[str, int] = {}
    for d in drug_summaries.values():
        if d["recommendation"]:
            by_rec[d["recommendation"]] = by_rec.get(d["recommendation"], 0) + 1
        if d["therapy_area"]:
            by_therapy[d["therapy_area"]] = by_therapy.get(d["therapy_area"], 0) + 1

    # Year-by-year volume of every extracted PSD record (not just one per drug)
    by_year: dict[str, int] = {}
    for row in good:
        yr = (row.get("pbac_year") or "").strip()
        if yr.isdigit() and 2000 <= int(yr) <= 2030:
            by_year[yr] = by_year.get(yr, 0) + 1

    total_recommended = by_rec.get("Recommended", 0) + by_rec.get("Recommended with restriction", 0)
    print(f"  Unique drugs: {len(drug_summaries)}  |  "
          f"Recommended: {total_recommended}  |  "
          f"Not recommended: {by_rec.get('Not recommended', 0)}")
    print(f"  Year coverage: {len(by_year)} years, {sum(by_year.values())} records")

    # ── Recently-published feed (top 50 PSDs by meeting date) ────────────────
    # One row per PSD record (not deduped by drug — a drug with two recent
    # submissions appears twice). Sort by (year, month) desc.
    recent: list[dict] = []
    for row in good:
        yr = _safe_int(row.get("pbac_year"))
        mo = _safe_int(row.get("pbac_month"))
        if not yr:
            continue
        drug = (row.get("drug") or "").strip().lower()
        if not drug:
            m = re.match(r'^(.+?)-psd-', (row.get("filename") or "").lower())
            drug = m.group(1).replace("-", " ") if m else ""
        if not drug:
            continue
        recent.append({
            "drug":           drug,
            "year":           str(yr),
            "month":          mo or 0,
            "recommendation": row.get("recommendation", ""),
            "listing_type":   row.get("listing_type", ""),
            "icer_low":       _safe_int(row.get("icer_low")),
            "icer_high":      _safe_int(row.get("icer_high")),
            "indication":     (row.get("indication") or "").strip()[:140],
            "therapy_area":   row.get("therapy_area", ""),
            "filename":       row.get("filename", ""),
        })
    recent.sort(key=lambda r: (int(r["year"]), r["month"] or 0), reverse=True)
    recent = recent[:50]
    if recent:
        print(f"  Recent feed   : {len(recent)} most-recent PSDs (latest {recent[0]['year']}-{recent[0]['month']:02d})")

    # ── "Drugs that earned their listing" (rejection → recommendation flips) ──
    # Walk per-drug history; pick drugs whose first PSD bucketed to "not" (or
    # deferred) and whose latest PSD bucketed to "rec". Highlight the most
    # recent flip-pair as the "story" delta. Sort by the year of the flip
    # (most recent first) so the homepage shows newer wins above older ones.
    flips: list[dict] = []
    for drug, summary in drug_summaries.items():
        hist = summary["history"]
        if len(hist) < 2:
            continue
        first_b = _outcome_bucket(hist[0]["rec"])
        last_b  = _outcome_bucket(hist[-1]["rec"])
        if first_b not in ("not", "deferred") or last_b != "rec":
            continue
        # Find the specific delta that flipped to rec (last "not"/"deferred" → first "rec")
        flip_delta = None
        for d in summary["deltas"]:
            if d["from_bucket"] in ("not", "deferred") and d["to_bucket"] == "rec":
                flip_delta = d
                break
        if not flip_delta:
            continue
        flips.append({
            "drug":          drug,
            "therapy_area":  summary["therapy_area"],
            "indication":    (summary["indication"] or "")[:140],
            "submissions":   summary["submissions"],
            "first_year":    summary["first_year"],
            "latest_year":   summary["latest_year"],
            "flip":          flip_delta,
        })
    # Most recent flip first
    flips.sort(
        key=lambda f: (int(f["flip"]["to_year"] or 0), int(f["flip"]["to_month"] or 0)),
        reverse=True,
    )
    if flips:
        print(f"  Flip stories  : {len(flips)} drugs went rejection/deferred → recommended")

    return {
        "total":             len(drug_summaries),
        "drugs":             drug_summaries,
        "by_recommendation": dict(sorted(by_rec.items(),     key=lambda x: -x[1])),
        "by_therapy":        dict(sorted(by_therapy.items(), key=lambda x: -x[1])),
        "by_year":           dict(sorted(by_year.items())),
        "recent":            recent,
        "flips":             flips,
    }


# ── 4. Nearest-neighbour links from embed_psds.py ────────────────────────────

def load_psd_nearest() -> dict:
    """Load data/psd_nearest.json (produced by embed_psds.py).

    Schema:
        { drug_name: [ {drug: <other_name>, score: <float>}, ... ] }
    """
    path = data_path("psd_nearest.json")
    if not path.exists():
        print("  psd_nearest.json not found — run embed_psds.py for 'Similar drugs' links")
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        out: dict = {}
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
    except (OSError, json.JSONDecodeError) as e:
        print(f"  Could not parse psd_nearest.json ({e})")
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
        valid = [n for n in nbrs if n["drug"] in drugs]
        if not valid:
            continue
        # Limit to 10 — the dashboard shows at most a handful, keeps the JS file small
        drug_summary["nearest"] = valid[:10]
        matched += 1
    if matched:
        print(f"  Attached nearest-neighbour lists to {matched} drugs")


# ── 5. Drug-level PBS spend ──────────────────────────────────────────────────

def load_drug_spend() -> dict:
    """Load pbs_drug_spend.csv (from fetch_pbs_drug_spend.py).

    Returns top drugs by spend and by cost-per-script, plus a per-drug lookup.
    """
    path = data_path("pbs_drug_spend.csv")
    if not path.exists():
        print("  pbs_drug_spend.csv not found — run fetch_pbs_drug_spend.py first")
        return {"total": 0, "top_spend": [], "top_cost_per_script": [], "by_drug": {}}

    # Drug names are already title-cased upstream (fetch_pbs_drug_spend.py).
    # We only strip trailing PBS markers (^^/^/*/#) here.
    raw_rows = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            benefit = _safe_int(row.get("gov_benefit_aud"))
            scripts = _safe_int(row.get("scripts"))
            drug = re.sub(r'[\^*#]+$', '', (row.get("drug_name") or "").strip()).strip()
            if not drug or benefit is None:
                continue
            raw_rows.append({
                "drug_name":       drug,
                "brand_name":      (row.get("brand_name") or "").strip(),
                "atc_code":        (row.get("atc_code") or "").strip(),
                "gov_benefit_aud": benefit,
                "scripts":         scripts or 0,
                "_best_b":         benefit,
                "report_year":     row.get("report_year", ""),
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
    top_spend = sorted(rows, key=lambda r: r["gov_benefit_aud"], reverse=True)[:20]
    top_cps = sorted(
        [r for r in rows if r.get("cost_per_script_aud") and (r.get("scripts") or 0) >= 50],
        key=lambda r: r["cost_per_script_aud"],
        reverse=True,
    )[:20]
    by_drug = {r["drug_name"].lower(): r for r in rows}

    total_benefit = sum(r["gov_benefit_aud"] for r in rows)
    print(f"  Drug spend: {len(rows)} drugs, total govt benefit ${total_benefit/1e9:.2f}B ({year})")
    if top_spend:
        print(f"    Top drug: {top_spend[0]['drug_name']} (${top_spend[0]['gov_benefit_aud']/1e6:.0f}M)")
    if top_cps:
        print(f"    Highest cost/script: {top_cps[0]['drug_name']} (${top_cps[0]['cost_per_script_aud']:,.0f})")

    return {
        "total":               len(rows),
        "report_year":         year,
        "top_spend":           top_spend,
        "top_cost_per_script": top_cps,
        "by_drug":             by_drug,
    }


# ── 6. PBAC cycle calendar ───────────────────────────────────────────────────

def load_pbac_calendar() -> dict:
    """Load data/pbac_calendar.json (produced by parse_pbac_calendar.py).

    Returns ``meetings`` sorted chronologically by code (YYYY-MM), plus a
    ``last_milestone`` date so the dashboard can flag a stale calendar.
    """
    path = data_path("pbac_calendar.json")
    if not path.exists():
        print("  pbac_calendar.json not found — run parse_pbac_calendar.py")
        return {"meetings": [], "last_updated": None, "last_milestone": None}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"  Could not parse pbac_calendar.json ({e})")
        return {"meetings": [], "last_updated": None, "last_milestone": None}

    meetings = data.get("meetings", []) or []
    # Sort chronologically by meeting code (YYYY-MM), with meeting_date as tiebreaker.
    # Sorting by earliest deadline puts e.g. March-meeting (subs deadline in Nov)
    # ahead of the November meeting it precedes — wrong chronologically.
    meetings.sort(key=lambda m: (m.get("code") or "", m.get("meeting_date") or ""))

    all_dates = [
        dl["date"]
        for m in meetings
        for dl in (m.get("deadlines") or [])
        if dl.get("date")
    ]
    last_milestone = max(all_dates) if all_dates else None

    print(f"  PBAC calendar: {len(meetings)} meetings, latest milestone {last_milestone or 'n/a'}")
    return {
        "meetings":       meetings,
        "last_updated":   data.get("last_updated"),
        "source_files":   data.get("source_files", []),
        "last_milestone": last_milestone,
    }
