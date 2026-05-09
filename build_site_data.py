"""
build_site_data.py
━━━━━━━━━━━━━━━━━━
Reads all available data sources and writes site_data.js —
a single JS file that embeds every dataset the site needs.

DATA SOURCES (any that exist are incorporated):
  atc_benefit.csv      — PBS government benefit by ATC class, year, state
  atc_services.csv     — PBS prescriptions by ATC class, year
  nice_metadata.csv    — NICE Technology Appraisals (scraped)
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
    benefit_path = HERE / "atc_benefit.csv"
    if not benefit_path.exists():
        print("  atc_benefit.csv not found — run parse_atc_data.py first")
        return result

    rows = []
    with open(benefit_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # Identify the grand-total series:
    # The third "Various +" group has values ~$1-18B, consistent with PBS totals.
    # Strategy: for each atc+year combination, keep all rows.
    # The grand total is the row where value is >= sum of all individual classes.
    # Simpler heuristic: the grand total row for 2024 is $18.39B (>$10B).

    # Collect by class, excluding TOTAL rows and YTD rows
    class_series: dict[str, dict] = {}  # atc_label -> {year_int -> value}

    # Track which rows seem like grand totals (value > 5B in 2024 era)
    GRAND_TOTAL_THRESHOLD = 5_000_000_000

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
        total = row.get("total")
        if not total:
            continue
        try:
            val = int(float(total))
        except (ValueError, TypeError):
            continue

        key = f"{atc}___{year}"  # composite key to detect duplicates
        if atc not in class_series:
            class_series[atc] = {}
        if year not in class_series[atc]:
            class_series[atc][year] = []
        class_series[atc][year].append(val)

    # Identify the grand total series: it has values > $1B even in early years
    # and > $10B in 2024. The genuine "Various" ATC class is < $500M/year.
    grand_total: dict[int, int] = {}
    individual_classes: dict[str, dict[int, int]] = {}

    GRAND_TOTAL_2024_MIN = 10_000_000_000  # $10B threshold

    # For each atc class, we may have multiple value series.
    # The grand total series is the one where 2024 value > $10B.
    for atc, year_vals in class_series.items():
        # Group into separate series by magnitude
        series_by_year: list[dict[int, int]] = []

        # Re-process: collect all values per year, then bucket by magnitude
        # We need to reconstruct the series properly.
        # Simple approach: if any year in 1992-2000 has value > $500M,
        # it's the grand total series (since individual classes were <$1B in 2000).

        # Actually: reprocess rows for this ATC class to split series
        pass  # handled below

    # Reprocess rows to split series properly
    # Group rows by their approximate magnitude bracket
    # The grand total grows from ~$2B (1994) to ~$18B (2024)
    # Individual ATC classes are much smaller

    series_groups: dict[str, list[dict]] = {}
    for row in rows:
        year_s = row.get("year", "")
        if year_s in ("TOTAL",) or "_ytd" in year_s:
            continue
        try:
            year = int(year_s)
        except ValueError:
            continue
        atc = row.get("atc", "").strip()
        total = row.get("total", "0")
        try:
            val = int(float(total))
        except:
            continue

        # Key: atc + magnitude bracket (grand total vs individual)
        is_grand = val > GRAND_TOTAL_THRESHOLD
        series_key = f"{atc}:::grand" if is_grand else f"{atc}:::individual"
        if series_key not in series_groups:
            series_groups[series_key] = []
        series_groups[series_key].append({"year": year, "val": val, "atc": atc})

    # Extract grand total from series
    for key, rows_s in series_groups.items():
        atc, kind = key.split(":::")
        by_year = {r["year"]: r["val"] for r in rows_s}
        if kind == "grand":
            # Merge into grand total (take max per year in case of overlap)
            for yr, val in by_year.items():
                if yr not in grand_total or val > grand_total[yr]:
                    grand_total[yr] = val
        else:
            # Individual ATC class — pick the series with more data
            label = atc
            if label not in individual_classes:
                individual_classes[label] = by_year
            else:
                # If duplicate, take the one with more years or higher values
                existing = individual_classes[label]
                if len(by_year) > len(existing):
                    individual_classes[label] = by_year
                elif len(by_year) == len(existing):
                    # Take higher-value series
                    avg_new = sum(by_year.values()) / max(len(by_year), 1)
                    avg_old = sum(existing.values()) / max(len(existing), 1)
                    if avg_new > avg_old:
                        individual_classes[label] = by_year

    # Convert to sorted year lists
    result["grand_total_by_year"] = {str(y): v for y, v in sorted(grand_total.items())}

    for atc_name, by_year in individual_classes.items():
        result["classes_by_year"][atc_name] = {str(y): v for y, v in sorted(by_year.items())}

    # ATC meta: which classes are present and their 2024 spend
    for atc_name, by_year in individual_classes.items():
        val_2024 = by_year.get(2024, 0)
        result["atc_meta"][atc_name] = {"spend_2024_aud": val_2024}

    print(f"  ATC benefit: {len(individual_classes)} individual classes, grand total {len(grand_total)} years")

    # ── Services data ─────────────────────────────────────────────────────────
    services_path = HERE / "atc_services.csv"
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
            except:
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
    for pdf in HERE.glob("*.pdf"):
        m = PSD_PATTERN.match(pdf.name.lower())
        if not m:
            continue
        drug, month_s, year_s = m.group(1), m.group(2), m.group(3)
        month = MONTH_MAP.get(month_s[:3])
        if not month:
            continue
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


# ── 3. PBAC–NICE Matched Data ────────────────────────────────────────────────

def load_matched_data() -> dict:
    """
    Load pbac_nice_matched.csv and produce per-drug summaries with access gap.

    Returns:
      {
        "total": int,          # number of unique matched drugs
        "drugs": [             # top 150 most interesting, sorted by |gap|
          { drug, therapy, pbac_date, nice_date, gap_months, nice_count,
            nice_tas: [{ta, title, rec, url, date}], nice_rec },
          ...
        ],
        "by_therapy": { therapy: {total, median_gap, nice_first, aus_first} },
        "stats": { median_gap_months, pct_nice_first, pct_aus_first, total_matched }
      }
    """
    path = HERE / "pbac_nice_matched.csv"
    if not path.exists():
        print("  pbac_nice_matched.csv not found — run match_pbac_nice.py first")
        return {"total": 0, "drugs": [], "by_therapy": {}, "stats": {}}

    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # Aggregate per unique drug name
    drug_map: dict[str, dict] = {}
    for row in rows:
        drug    = row.get("drug", "").strip().lower()
        therapy = row.get("therapy_area", "").strip()
        pbac_d  = row.get("pbac_date", "").strip()
        nice_d  = row.get("nice_date", "").strip()
        nice_ta = row.get("nice_ta_number", "").strip()
        nice_rec= row.get("nice_recommendation", "").strip()
        nice_url= row.get("nice_guidance_url", "").strip()
        nice_ttl= row.get("nice_title", "").strip()
        try:
            gap = float(row.get("gap_months", 0) or 0)
        except (ValueError, TypeError):
            gap = 0.0

        if not drug:
            continue
        if drug not in drug_map:
            drug_map[drug] = {
                "drug":      drug,
                "therapy":   therapy,
                "pbac_dates": [],
                "nice_dates": [],
                "tas":       {},   # ta_number -> {ta, title, rec, url, date}
            }
        d = drug_map[drug]
        if pbac_d and pbac_d not in d["pbac_dates"]:
            d["pbac_dates"].append(pbac_d)
        if nice_d and nice_d not in d["nice_dates"]:
            d["nice_dates"].append(nice_d)
        if nice_ta and nice_ta not in d["tas"]:
            d["tas"][nice_ta] = {
                "ta":    nice_ta,
                "title": nice_ttl[:80],
                "rec":   nice_rec[:40] if nice_rec else "",
                "url":   nice_url,
                "date":  nice_d,
            }

    # Build summary per drug
    drug_list: list[dict] = []
    for drug, d in drug_map.items():
        pbac_dates = sorted(d["pbac_dates"])
        nice_dates = sorted(d["nice_dates"])
        first_pbac = pbac_dates[0] if pbac_dates else None
        first_nice = nice_dates[0] if nice_dates else None

        # Gap = PBAC first date minus NICE first date (in months)
        rep_gap = 0.0
        if first_pbac and first_nice:
            try:
                pd_dt = datetime.strptime(first_pbac, "%Y-%m-%d")
                nd_dt = datetime.strptime(first_nice, "%Y-%m-%d")
                rep_gap = round((pd_dt - nd_dt).days / 30.44, 1)
            except ValueError:
                pass

        # Best NICE recommendation (prefer "Recommended" over "Not recommended")
        tas_sorted = sorted(d["tas"].values(), key=lambda x: x["date"])
        best_rec = ""
        for ta in tas_sorted:
            r = ta.get("rec", "")
            if r.startswith("Recommended"):
                best_rec = r
                break
        if not best_rec:
            for ta in tas_sorted:
                if ta.get("rec"):
                    best_rec = ta["rec"]
                    break

        drug_list.append({
            "drug":       drug,
            "therapy":    d["therapy"],
            "pbac_date":  first_pbac,
            "nice_date":  first_nice,
            "gap_months": rep_gap,
            "nice_count": len(d["tas"]),
            "nice_tas":   tas_sorted[:4],   # up to 4 TAs per drug
            "nice_rec":   best_rec[:40] if best_rec else "",
        })

    # Sort by |gap| descending (most divergent first = most interesting)
    drug_list.sort(key=lambda x: -abs(x["gap_months"]))

    # Stats
    all_gaps = [d["gap_months"] for d in drug_list]
    median_gap = sorted(all_gaps)[len(all_gaps)//2] if all_gaps else 0
    nice_first_n = sum(1 for g in all_gaps if g > 0)
    aus_first_n  = sum(1 for g in all_gaps if g < 0)
    n = len(drug_list)

    # By therapy
    by_therapy: dict[str, dict] = {}
    for d in drug_list:
        t = d["therapy"] or "Other"
        if t not in by_therapy:
            by_therapy[t] = {"total": 0, "gaps": [], "nice_first": 0, "aus_first": 0}
        by_therapy[t]["total"] += 1
        by_therapy[t]["gaps"].append(d["gap_months"])
        if d["gap_months"] > 0: by_therapy[t]["nice_first"] += 1
        elif d["gap_months"] < 0: by_therapy[t]["aus_first"] += 1

    by_therapy_out = {}
    for t, v in sorted(by_therapy.items(), key=lambda x: -x[1]["total"]):
        g = sorted(v["gaps"])
        med = round(g[len(g)//2], 1) if g else 0
        by_therapy_out[t] = {
            "total":       v["total"],
            "median_gap":  med,
            "nice_first":  v["nice_first"],
            "aus_first":   v["aus_first"],
        }

    print(f"  Matched: {n} unique drugs, median gap {round(median_gap,1)} months, "
          f"{round(nice_first_n/n*100) if n else 0}% UK first")

    return {
        "total":       n,
        "drugs":       drug_list[:150],  # top 150 most divergent
        "by_therapy":  by_therapy_out,
        "stats": {
            "median_gap_months":  round(median_gap, 1),
            "pct_nice_first":     round(nice_first_n / n * 100) if n else 0,
            "pct_aus_first":      round(aus_first_n  / n * 100) if n else 0,
            "total_matched":      n,
        },
    }


# ── 4. PSD Extracted Data ─────────────────────────────────────────────────────

def load_psd_extracted() -> dict:
    """
    Load psd_extracted.csv (from extract_psd_text.py).
    Groups by drug name, produces per-drug summaries with submission history.
    """
    path = HERE / "psd_extracted.csv"
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
        }

    by_rec: dict[str, int] = {}
    by_therapy: dict[str, int] = {}
    for d in drug_summaries.values():
        r = d["recommendation"]
        if r: by_rec[r] = by_rec.get(r, 0) + 1
        t = d["therapy_area"]
        if t: by_therapy[t] = by_therapy.get(t, 0) + 1

    total_recommended = by_rec.get("Recommended", 0) + by_rec.get("Recommended with restriction", 0)
    total_with_rec    = sum(by_rec.values())
    print(f"  Unique drugs: {len(drug_summaries)}  |  "
          f"Recommended: {total_recommended}  |  "
          f"Not recommended: {by_rec.get('Not recommended', 0)}")

    return {
        "total":            len(drug_summaries),
        "drugs":            drug_summaries,
        "by_recommendation": dict(sorted(by_rec.items(),    key=lambda x: -x[1])),
        "by_therapy":        dict(sorted(by_therapy.items(), key=lambda x: -x[1])),
    }


# ── 5. NICE Metadata ──────────────────────────────────────────────────────────

# ── Therapy area tagging ──────────────────────────────────────────────────────

# Keywords → canonical therapy area label
THERAPY_TAGS: list[tuple[list[str], str]] = [
    (["cancer","carcinoma","tumour","tumor","melanoma","lymphoma","leukaemia","leukemia",
      "myeloma","sarcoma","glioma","glioblastoma","mesothelioma","neuroblastoma",
      "neoplasm","malignant","malignancy","oncol","antineoplastic","checkpoint"],
     "Oncology"),
    (["breast cancer","breast"],          "Breast cancer"),
    (["lung cancer","non-small-cell","nsclc","small-cell","sclc","mesothelioma"],
     "Lung cancer"),
    (["prostate"],                         "Prostate cancer"),
    (["colorectal","colon cancer","rectal"], "Colorectal cancer"),
    (["leukaemia","leukemia","lymphoma","myeloma","multiple myeloma","hodgkin","cll","cml"],
     "Haematology"),
    (["renal cell","kidney cancer","renal cancer"],  "Renal cancer"),
    (["ovarian","fallopian","peritoneal"],  "Ovarian cancer"),
    (["bladder"],                           "Bladder cancer"),
    (["melanoma"],                          "Melanoma"),
    (["head and neck"],                     "Head & neck cancer"),
    (["rheumatoid arthritis","psoriatic arthritis","ankylosing spondylitis",
      "axial spondyloarthritis","juvenile idiopathic"],
     "Inflammatory arthritis"),
    (["psoriasis","plaque psoriasis"],      "Psoriasis"),
    (["crohn","ulcerative colitis","inflammatory bowel"],
     "Inflammatory bowel disease"),
    (["atopic dermatitis","eczema"],        "Atopic dermatitis"),
    (["asthma","copd","pulmonary"],         "Respiratory"),
    (["multiple sclerosis","relapsing","secondary progressive"],
     "Multiple sclerosis"),
    (["diabetes","glycaemic","insulin","semaglutide","glp-1","sglt"],
     "Diabetes"),
    (["heart failure","cardiac","atrial fibrillation","anticoagul",
      "thromboembolism","venous thrombosis","pulmonary embolism"],
     "Cardiovascular"),
    (["alzheimer","dementia","parkinson"],  "Neurology"),
    (["hiv","antiretroviral"],              "HIV"),
    (["hepatitis"],                         "Hepatitis"),
    (["rare disease","orphan","gaucher","fabry","pompe","spinal muscular",
      "duchenne","cystic fibrosis","lysosomal","enzyme replacement"],
     "Rare disease"),
    (["osteoporosis","bone"],               "Musculo-skeletal"),
    (["age-related macular","wet amd","macular degeneration","retinal"],
     "Ophthalmology"),
    (["migraine","epilepsy","seizure"],     "Neurology"),
]

def extract_tags(title: str, drug: str) -> list[str]:
    """Extract therapy-area tags from a NICE title or drug name."""
    t = (title + " " + drug).lower()
    tags: list[str] = []
    seen: set[str] = set()
    for keywords, label in THERAPY_TAGS:
        if label in seen:
            continue
        if any(kw in t for kw in keywords):
            tags.append(label)
            seen.add(label)
            # Top-level Oncology tag for all cancer sub-types
            if label not in ("Oncology",) and any(
                kw in t for kw in ["cancer","carcinoma","tumour","tumor","lymphoma",
                                   "leukaemia","leukemia","myeloma","melanoma","sarcoma"]
            ):
                if "Oncology" not in seen:
                    tags.insert(0, "Oncology")
                    seen.add("Oncology")
    return tags


def load_nice_data() -> dict:
    """Load nice_metadata.csv and build a rich search index."""
    path = HERE / "nice_metadata.csv"
    if not path.exists():
        return {"total": 0, "by_year": {}, "search_index": []}

    rows = []
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    by_year: dict[str, int] = {}
    by_therapy: dict[str, int] = {}
    search_index: list[dict] = []

    for row in rows:
        ta_raw  = row.get("ta_number", "").strip()
        title   = row.get("title", "").strip()
        drug    = row.get("drug_name", "").strip()
        date_s  = row.get("published_date", "").strip()
        rec     = row.get("recommendation", "").strip()
        url     = row.get("guidance_url", "").strip()

        # Year
        yr_m = re.search(r'\b(\d{4})\b', date_s)
        year = int(yr_m.group(1)) if yr_m else None
        if year:
            yr_s = str(year)
            by_year[yr_s] = by_year.get(yr_s, 0) + 1

        # Therapy tags
        tags = extract_tags(title, drug)
        for tag in tags:
            by_therapy[tag] = by_therapy.get(tag, 0) + 1

        # Extract indication from title ("for treating X" or "for X")
        indication = ""
        ind_m = re.search(r'\bfor\s+(?:treating\s+|the\s+treatment\s+of\s+|preventing\s+)?(.+?)(?:\s*[-–(]|$)',
                          title, re.IGNORECASE)
        if ind_m:
            indication = ind_m.group(1).strip()[:100]

        # Build search entry
        entry: dict = {
            "ta":   int(ta_raw) if ta_raw.isdigit() else 0,
            "id":   f"ta{ta_raw}",
            "title": title,
            "drug": drug,
            "indication": indication,
            "year": year,
            "tags": tags,
            "rec":  rec[:60] if rec else "",
            "url":  url,
        }
        search_index.append(entry)

    # Sort by TA number
    search_index.sort(key=lambda x: x["ta"])

    ta_max = max((r["ta"] for r in search_index if r["ta"] > 0), default=0)
    print(f"  NICE TAs: {len(rows)} appraisals, {len(by_year)} years, {len(set(t for e in search_index for t in e['tags']))} therapy areas")
    return {
        "total":        len(rows),
        "by_year":      dict(sorted(by_year.items())),
        "by_therapy":   dict(sorted(by_therapy.items(), key=lambda x: -x[1])),
        "ta_range":     [1, ta_max],
        "search_index": search_index,
    }


# ── 6. Drug-level PBS spend ───────────────────────────────────────────────────

def load_drug_spend() -> dict:
    """
    Load pbs_drug_spend.csv (from fetch_pbs_drug_spend.py).
    Returns top drugs by spend and by cost-per-script.
    """
    path = HERE / "pbs_drug_spend.csv"
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

    rows = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            benefit = _int(row.get("gov_benefit_aud"))
            scripts = _int(row.get("scripts"))
            cps     = _float(row.get("cost_per_script_aud"))
            drug    = (row.get("drug_name") or "").strip()
            if not drug or benefit is None:
                continue
            rows.append({
                "drug_name":          drug,
                "brand_name":         (row.get("brand_name") or "").strip(),
                "atc_code":           (row.get("atc_code") or "").strip(),
                "gov_benefit_aud":    benefit,
                "scripts":            scripts,
                "cost_per_script_aud": cps,
                "report_year":        row.get("report_year", ""),
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

def write_site_data(pbs: dict, pbac: dict, psd: dict, nice: dict, matched: dict,
                    drug_spend: dict | None = None):
    now = datetime.now().strftime("%Y-%m-%d")

    # Compute key stats for the stats bar
    pbs_2024   = pbs["grand_total_by_year"].get("2024", 0)
    pbs_1994   = pbs["grand_total_by_year"].get("1994", 1)
    pbs_growth = round(pbs_2024 / pbs_1994, 1) if pbs_1994 else 0

    scripts_2024 = pbs["services_by_year"].get("2024", 0)

    site_data = {
        "generated": now,
        "stats": {
            "pbac_psds":         pbac["total"],
            "pbac_years":        pbac["year_range"],
            "nice_tas":          nice["total"],
            "pbs_2024_aud":      pbs_2024,
            "pbs_growth_x":      pbs_growth,
            "scripts_2024":      scripts_2024,
            "matched_drugs":     matched.get("total", 0),
            "median_gap_months": matched.get("stats", {}).get("median_gap_months", 0),
            "pct_nice_first":    matched.get("stats", {}).get("pct_nice_first", 0),
        },
        "pbs": {
            "grand_total_by_year": pbs["grand_total_by_year"],
            "services_by_year":    pbs["services_by_year"],
            "classes_by_year":     pbs["classes_by_year"],
            "atc_meta":            pbs["atc_meta"],
        },
        "pbac":       pbac,
        "psd":        psd,
        "nice":       nice,
        "matched":    matched,
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

    print("\n[1/6] Loading PBS ATC data...")
    pbs = load_atc_data()

    print("\n[2/6] Scanning PBAC PSDs...")
    pbac = load_pbac_psds()

    print("\n[3/6] Loading extracted PSD data...")
    psd = load_psd_extracted()

    print("\n[4/6] Loading NICE metadata...")
    nice = load_nice_data()

    print("\n[5/6] Loading PBAC–NICE matched data...")
    matched = load_matched_data()

    print("\n[6/6] Loading PBS drug-level spend...")
    drug_spend = load_drug_spend()

    print("\nWriting output...")
    write_site_data(pbs, pbac, psd, nice, matched, drug_spend)

    # Summary
    gt = pbs["grand_total_by_year"]
    print("\n" + "=" * 60)
    print("KEY STATS")
    print("=" * 60)
    print(f"  PBS total 1994     : ${pbs['grand_total_by_year'].get('1994',0)/1e9:.2f}B")
    print(f"  PBS total 2024     : ${pbs['grand_total_by_year'].get('2024',0)/1e9:.2f}B")
    print(f"  Growth             : {pbs['grand_total_by_year'].get('2024',0)/max(pbs['grand_total_by_year'].get('1994',1),1):.1f}x")
    print(f"  Scripts 2024       : {pbs['services_by_year'].get('2024',0)/1e6:.1f}M")
    print(f"  PBAC PSDs          : {pbac['total']}")
    print(f"  Extracted drugs    : {psd['total']}")
    rec_counts = psd.get('by_recommendation', {})
    for rec, n in rec_counts.items():
        print(f"    {rec:<35} {n}")
    print(f"  NICE TAs           : {nice['total']}")
    ms = matched.get("stats", {})
    print(f"  Matched drugs      : {matched.get('total', 0)}")
    print(f"  Median access gap  : {ms.get('median_gap_months', 0)} months")
    print(f"  UK moves first     : {ms.get('pct_nice_first', 0)}%")
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
