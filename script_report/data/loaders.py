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
from datetime import date

from script_report.config import DATA_DIR, REPO_ROOT
from script_report.utils.helpers import MONTH_MAP, data_path
from script_report.utils.drug_names import candidate_keys


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


_DOMINANT_RE = re.compile(
    r"\b(?:dominant|dominate[sd]?|dominance|"
    r"less\s+(?:costly|expensive)\s+and\s+more\s+effective|"
    r"more\s+effective\s+and\s+less\s+(?:costly|expensive)|"
    r"cost\s*[-\s]?\s*saving)\b",
    re.I,
)
_COST_NEUTRAL_RE = re.compile(r"\bcost\s*[-\s]?\s*neutral\b", re.I)
_COST_MIN_RE = re.compile(r"cost[-\s]?minimisat", re.I)
_REDACTED_RE = re.compile(
    r"\b(?:redacted|commercial[-\s]?in[-\s]?confidence|\bcic\b|withheld|"
    r"not\s+(?:disclosed|stated|reported|provided|publicly\s+available)|"
    r"removed\s+from\s+the\s+psd)\b",
    re.I,
)
_NOT_MODELLED_RE = re.compile(
    r"\b(?:not\s+modelled|no\s+(?:economic\s+)?(?:model|evaluation)|"
    r"not\s+(?:calculated|applicable)|bia\s+only|budget\s+impact\s+only)\b",
    re.I,
)


def _normalise_entity(s: str) -> str:
    """Lowercase, collapse whitespace, strip surrounding punctuation. Used for
    keying comparator / trial backlinks so equivalent strings collapse."""
    s = (s or "").lower().strip().strip(".,;:")
    s = re.sub(r"\s+", " ", s)
    return s


def _split_comparators(s: str) -> list[str]:
    """Split a comparator string into atomic candidates plus the original whole.

    "docetaxel + carboplatin" -> ["docetaxel + carboplatin", "docetaxel", "carboplatin"]
    "docetaxel or BSC" -> ["docetaxel or BSC", "docetaxel", "BSC"]
    """
    full = (s or "").strip()
    if not full:
        return []
    out = [full]
    components = re.split(r"\s*(?:[+,&/]| or | and )\s*", full, flags=re.IGNORECASE)
    for c in components:
        c = c.strip()
        if c and c.lower() != full.lower() and len(c) >= 3:
            out.append(c)
    return out


def _split_trials(s: str) -> list[str]:
    """Split a comma/semicolon-separated trial-IDs string."""
    full = (s or "").strip()
    if not full:
        return []
    return [p.strip() for p in re.split(r"\s*[,;]\s*", full) if p.strip() and len(p.strip()) >= 3]


def _classify_cost_basis(row: dict) -> str:
    """Classify a single PSD row's economic basis.

    Returns one of: numeric, dominant, cost_neutral, cost_minimisation,
    redacted, not_modelled, unknown.

    The dashboard shows "—" wherever there's no numeric ICER, which
    over-reports those drugs as "data missing" — but ~90% of the corpus
    has no numeric ICER, almost all because the framing is something
    other than an ICER (cost-minimisation, dominance, redaction, etc.).
    This classifier picks up that framing from icer_note / economic_model
    so the dashboard can show the actual basis.
    """
    icer_low  = _safe_int(row.get("icer_low"))
    icer_high = _safe_int(row.get("icer_high"))
    if icer_low or icer_high:
        return "numeric"

    em   = (row.get("economic_model") or "").strip()
    note = (row.get("icer_note") or "").strip()
    blob = note + " " + em   # search both as one string

    # Dominance language is the most informative signal — check first.
    if _DOMINANT_RE.search(blob):
        return "dominant"
    if _COST_NEUTRAL_RE.search(blob):
        return "cost_neutral"
    if _COST_MIN_RE.search(blob):
        return "cost_minimisation"
    if _REDACTED_RE.search(blob):
        return "redacted"
    if _NOT_MODELLED_RE.search(blob):
        return "not_modelled"
    return "unknown"


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


# ── Sponsor name normalisation ──────────────────────────────────────────────
# PBAC PSDs vary in how they print sponsor names ("Pfizer Australia", "Pfizer
# Australia Pty Ltd", "Pfizer Pty Limited"). Strip a conservative set of
# trailing legal / territorial tokens to collapse those variants into one
# group, while displaying the most common verbatim form back to the user.
_SPONSOR_LEGAL_RE = re.compile(
    r"\s+(?:pty\s+ltd|pty\.?\s+limited|pty\.?|ltd\.?|limited|"
    r"inc\.?|incorporated|llc|"
    r"australia|australasia|oceania|aus\.?|au|"
    # Generic corporate suffixes that don't denote a distinct entity.
    # Deliberately excludes "healthcare" — e.g. Merck Healthcare (German)
    # is a different company from Merck Sharp & Dohme.
    r"pharmaceuticals|pharmaceutical|pharma|products)\b\.?$",
    re.I,
)

# Trailing parentheticals like "Merck Sharp & Dohme (Australia)" or "BeiGene (AU)"
_SPONSOR_PAREN_RE = re.compile(r"\s*\([^()]+\)\s*$")


def _sponsor_key(s: str) -> str:
    """Lowercased, suffix-stripped grouping key for a sponsor string.

    Also normalises common formatting variants so 'Janssen-Cilag' and
    'Janssen Cilag', or 'Bristol-Myers Squibb' and 'Bristol Myers Squibb',
    collapse into the same group.
    """
    if not s:
        return ""
    out = s.strip()
    # Repeat parenthetical + legal-suffix stripping until stable (some sponsors
    # have both, e.g. "Merck Sharp & Dohme (Australia) Pty Ltd").
    for _ in range(5):
        new = _SPONSOR_PAREN_RE.sub("", out)
        new = _SPONSOR_LEGAL_RE.sub("", new).strip().rstrip(",.")
        if new == out:
            break
        out = new
    out = out.lower()
    # Hyphens/en-dashes/em-dashes/slashes → space; "&" → "and"; collapse spaces
    out = re.sub(r"[–—‐\-/]+", " ", out)
    out = re.sub(r"\s+&\s+", " and ", out)
    out = re.sub(r"[.,()]+", " ", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def _compute_sponsor_stats(rows: list[dict], drug_summaries: dict[str, dict]) -> dict:
    """Aggregate per-sponsor counts from raw extracted rows.

    Each row = one PSD submission. Win rate is computed at the submission
    level (rec bucket / total bucketable). Per-drug counts come from
    drug_summaries' latest-decision view.

    Drug ownership for spend attribution: a drug's PBS spend is credited
    to a single sponsor — the one with the most recent Recommended
    submission for that drug, falling back to the most recent submission
    overall. This avoids double-counting biosimilars where several
    sponsors have submitted the same molecule.

    Returns a dict keyed by sponsor_key with:
        display           most-common verbatim form
        n_submissions     total submissions tagged to this sponsor
        n_recommended     submissions whose outcome bucketed to 'rec'
        n_deferred        submissions whose outcome bucketed to 'deferred'
        n_not             submissions whose outcome bucketed to 'not'
        drug_keys         all drugs this sponsor has submitted
        owned_drug_keys   drugs this sponsor "owns" for spend (see above)
        therapy_areas     {area: count}
        last_year         most recent year on file
    """
    out: dict[str, dict] = {}
    drug_latest_bucket: dict[str, str] = {
        k: _outcome_bucket(s.get("recommendation"))
        for k, s in drug_summaries.items()
    }
    # Per-drug submission log used for ownership assignment (drug → list of
    # (sponsor_key, bucket, year, month)). Built in the same pass so we
    # don't iterate rows twice.
    drug_subs: dict[str, list[tuple[str, str, int, int]]] = {}

    for row in rows:
        sponsor = (row.get("sponsor") or "").strip()
        if not sponsor:
            continue
        key = _sponsor_key(sponsor)
        if not key:
            continue

        drug = (row.get("drug") or "").strip().lower()
        if not drug:
            m = re.match(r'^(.+?)-psd-', (row.get("filename") or "").lower())
            drug = m.group(1).replace("-", " ") if m else ""
        if not drug:
            continue

        bucket = _outcome_bucket(row.get("recommendation"))
        therapy = (row.get("therapy_area") or "").strip()
        yr = _safe_int(row.get("pbac_year")) or 0
        mo = _safe_int(row.get("pbac_month")) or 0

        entry = out.setdefault(key, {
            "display":          sponsor,
            "_display_counts":  {},
            "n_submissions":    0,
            "n_recommended":    0,
            "n_deferred":       0,
            "n_not":            0,
            "drug_keys":        set(),
            "owned_drug_keys":  set(),
            "therapy_areas":    {},
            "last_year":        0,
            "first_year":       0,
            "years":            {},                 # {year: submission count}
            "drug_sub_counts":  {},                 # {drug: submissions-by-this-sponsor}
            "rejection_reasons": [],                # raw strings from not-rec rows
        })
        entry["_display_counts"][sponsor] = entry["_display_counts"].get(sponsor, 0) + 1
        entry["n_submissions"] += 1
        if bucket == "rec":      entry["n_recommended"] += 1
        elif bucket == "not":    entry["n_not"] += 1
        elif bucket == "deferred": entry["n_deferred"] += 1
        entry["drug_keys"].add(drug)
        entry["drug_sub_counts"][drug] = entry["drug_sub_counts"].get(drug, 0) + 1
        if therapy:
            entry["therapy_areas"][therapy] = entry["therapy_areas"].get(therapy, 0) + 1
        if yr > entry["last_year"]:
            entry["last_year"] = yr
        if yr and (entry["first_year"] == 0 or yr < entry["first_year"]):
            entry["first_year"] = yr
        if yr:
            entry["years"][yr] = entry["years"].get(yr, 0) + 1
        if bucket == "not":
            reasons = (row.get("rejection_reasons") or "").strip()
            if reasons:
                entry["rejection_reasons"].append(reasons)

        drug_subs.setdefault(drug, []).append((key, bucket, yr, mo))

    # Assign each drug to one owning sponsor: most-recent 'rec' wins; if no
    # 'rec' on file, fall back to the most-recent submission of any kind.
    for drug, subs in drug_subs.items():
        subs.sort(key=lambda r: (r[2], r[3]), reverse=True)
        owner = next((r[0] for r in subs if r[1] == "rec"), None)
        if owner is None:
            owner = subs[0][0]
        if owner in out:
            out[owner]["owned_drug_keys"].add(drug)

    # Pick the most-common display form, and fill in drug-level counts (drugs
    # currently listed = drugs whose latest decision bucketed to 'rec').
    for key, entry in out.items():
        entry["display"] = max(entry["_display_counts"].items(), key=lambda kv: kv[1])[0]
        del entry["_display_counts"]
        drug_keys = entry["drug_keys"]
        entry["n_drugs"] = len(drug_keys)
        entry["n_drugs_listed"] = sum(
            1 for d in drug_keys if drug_latest_bucket.get(d) == "rec"
        )
        # Top therapy area by submission count
        if entry["therapy_areas"]:
            entry["top_therapy_area"] = max(
                entry["therapy_areas"].items(), key=lambda kv: kv[1]
            )[0]
        else:
            entry["top_therapy_area"] = ""

    return out


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
            "cost_basis":        _classify_cost_basis(latest),
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

    # ── Comparator + trial inverse indexes (backlinks) ───────────────────────
    # For each drug, walk its comparator + key_trials fields, normalise into
    # atomic candidates, and add the drug to that candidate's index entry.
    # Only ship entries that ended up with >=2 drugs — singletons have nothing
    # to "link to" beyond the drug we're already on.
    comparator_index_raw: dict[str, dict] = {}
    trial_index_raw:      dict[str, dict] = {}
    for drug, summary in drug_summaries.items():
        for c in _split_comparators(summary.get("comparator") or ""):
            key = _normalise_entity(c)
            if not key:
                continue
            entry = comparator_index_raw.setdefault(key, {"display": c, "drugs": set()})
            entry["drugs"].add(drug)
        for t in _split_trials(summary.get("key_trials") or ""):
            key = _normalise_entity(t)
            if not key:
                continue
            entry = trial_index_raw.setdefault(key, {"display": t, "drugs": set()})
            entry["drugs"].add(drug)

    comparator_index = {
        k: {"display": v["display"], "drugs": sorted(v["drugs"])}
        for k, v in comparator_index_raw.items()
        if len(v["drugs"]) >= 2
    }
    trial_index = {
        k: {"display": v["display"], "drugs": sorted(v["drugs"])}
        for k, v in trial_index_raw.items()
        if len(v["drugs"]) >= 2
    }
    print(f"  Comparator backlinks: {len(comparator_index)} comparators referenced by 2+ drugs")
    print(f"  Trial backlinks     : {len(trial_index)} trials referenced by 2+ drugs")

    # ── Time from first submission to first "Recommended" outcome ────────────
    # Most drugs are recommended first try (zero delay). The editorially
    # interesting cohort is the minority that needs multiple submissions.
    # Report both an overall headline (% first try) and a per-year median for
    # multi-attempt drugs so the time-trend is meaningful.
    def _mo_index(h: dict) -> int:
        try:
            yr = int(h.get("year") or 0); mo = int(h.get("month") or 0)
            return yr * 12 + mo
        except (ValueError, TypeError):
            return 0

    multi_delays_by_year: dict[int, list[int]] = {}
    n_total = 0
    n_first_try = 0
    n_first_try_with_followups = 0
    multi_delays: list[int] = []
    for drug, summary in drug_summaries.items():
        hist = summary.get("history") or []
        if not hist:
            continue
        rec_idx = next(
            (i for i, h in enumerate(hist) if _outcome_bucket(h.get("rec")) == "rec" and _mo_index(h) > 0),
            None,
        )
        if rec_idx is None:
            continue
        first_mo = _mo_index(hist[0])
        rec_mo   = _mo_index(hist[rec_idx])
        if first_mo <= 0 or rec_mo <= 0:
            continue
        months = rec_mo - first_mo
        n_total += 1
        if months == 0:
            n_first_try += 1
            # Drugs that succeeded first try but returned later for new
            # indications, restriction tweaks, or price reviews.
            if (summary.get("submissions") or 1) > 1:
                n_first_try_with_followups += 1
            continue
        multi_delays.append(months)
        rec_year = hist[rec_idx].get("year")
        if rec_year and str(rec_year).isdigit():
            multi_delays_by_year.setdefault(int(rec_year), []).append(months)

    def _median(vals: list[int]) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        n = len(s)
        return float(s[n // 2]) if n % 2 == 1 else (s[n // 2 - 1] + s[n // 2]) / 2

    by_year_summary = {}
    for yr in sorted(multi_delays_by_year):
        vals = multi_delays_by_year[yr]
        if len(vals) >= 3:   # require >=3 drugs/year so the median isn't just one drug
            by_year_summary[str(yr)] = {
                "n":      len(vals),
                "median": _median(vals),
                "min":    min(vals),
                "max":    max(vals),
            }

    time_to_listing = {
        "n_with_recommendation": n_total,
        "n_first_try":           n_first_try,
        "pct_first_try":         (n_first_try * 100 // n_total) if n_total else 0,
        "n_first_try_with_followups": n_first_try_with_followups,
        "pct_first_try_with_followups": (
            (n_first_try_with_followups * 100 // n_first_try) if n_first_try else 0
        ),
        "n_multi_attempt":       len(multi_delays),
        "median_months_multi":   _median(multi_delays),
        "min_months_multi":      min(multi_delays) if multi_delays else 0,
        "max_months_multi":      max(multi_delays) if multi_delays else 0,
        "by_year":               by_year_summary,
    }
    print(f"  Time to listing: {time_to_listing['pct_first_try']}% first try; "
          f"resubmission median {time_to_listing['median_months_multi']:.0f} mo "
          f"({len(multi_delays)} drugs)")

    # ── Sponsor stats (raw — spend joined later by attach_sponsor_spend) ─────
    sponsors_raw = _compute_sponsor_stats(good, drug_summaries)
    n_with_sponsor = sum(s["n_submissions"] for s in sponsors_raw.values())
    print(f"  Sponsors      : {len(sponsors_raw)} unique sponsors across "
          f"{n_with_sponsor:,} submissions")

    return {
        "total":             len(drug_summaries),
        "drugs":             drug_summaries,
        "by_recommendation": dict(sorted(by_rec.items(),     key=lambda x: -x[1])),
        "by_therapy":        dict(sorted(by_therapy.items(), key=lambda x: -x[1])),
        "by_year":           dict(sorted(by_year.items())),
        "recent":            recent,
        "flips":             flips,
        "comparator_index":  comparator_index,
        "trial_index":       trial_index,
        "time_to_listing":   time_to_listing,
        "sponsors_raw":      sponsors_raw,
    }


# ── 4. Nearest-neighbour links from embed_psds.py ────────────────────────────

def load_psd_map() -> dict:
    """Load data/psd_map.json (produced by map_builder.py).

    Schema:
        { schema: [...], n: int, points: [[x, y, name, therapy, outcome_bucket, year], ...] }
    """
    path = data_path("psd_map.json")
    if not path.exists():
        print("  psd_map.json not found — run `python -m script_report map` to enable the cosmos plot")
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "points" not in data:
            return {}
        return data
    except Exception as e:
        print(f"  Failed to load psd_map.json: {e}")
        return {}


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


def attach_sponsor_spend(psd: dict, drug_spend: dict | None) -> None:
    """Join sponsor stats with PBS drug spend and finalise psd['sponsors'].

    Reads psd['sponsors_raw'] (built by load_psd_extracted) and produces a
    sorted list of leaderboard entries — one per sponsor — keyed by total
    PBS govt benefit captured. Spend is the sum of FY-latest govt benefit
    across each sponsor's drugs as found in drug_spend['by_drug'] (newer
    drugs without spend yet contribute 0).

    No-ops cleanly if the sponsor column is empty (e.g. before backfill).
    """
    if not psd:
        return
    raw = psd.get("sponsors_raw") or {}
    drugs_map = psd.get("drugs") or {}
    if not raw:
        psd["sponsors"] = []
        psd["sponsor_details"] = {}
        return

    by_drug = (drug_spend or {}).get("by_drug") or {}

    leaderboard: list[dict] = []
    details: dict[str, dict] = {}

    for key, entry in raw.items():
        # Spend attribution: only owned drugs (see _compute_sponsor_stats).
        owned = entry.get("owned_drug_keys") or set()
        per_drug_spend: dict[str, int] = {}
        total_spend = 0
        for d in owned:
            sp = by_drug.get(d.lower())
            if sp and sp.get("gov_benefit_aud"):
                amt = int(sp["gov_benefit_aud"])
                total_spend += amt
                per_drug_spend[d] = amt

        n_subs = entry["n_submissions"]
        n_rec  = entry["n_recommended"]
        bucketable = n_rec + entry["n_not"] + entry["n_deferred"]
        pct_rec = round(n_rec * 100 / bucketable) if bucketable else None

        # ── Slim leaderboard entry ────────────────────────────────────────
        sorted_pairs = sorted(per_drug_spend.items(), key=lambda kv: kv[1], reverse=True)
        leaderboard.append({
            "sponsor":           entry["display"],
            "key":               key,
            "n_submissions":     n_subs,
            "n_drugs":           entry["n_drugs"],
            "n_drugs_listed":    entry["n_drugs_listed"],
            "n_recommended":     n_rec,
            "n_not":             entry["n_not"],
            "n_deferred":        entry["n_deferred"],
            "pct_recommended":   pct_rec,
            "top_therapy_area":  entry["top_therapy_area"],
            "last_year":         entry["last_year"],
            "total_spend_aud":   total_spend,
            "top_drugs":         [
                {"drug": d, "spend_aud": v} for d, v in sorted_pairs[:3]
            ],
        })

        # ── Rich detail blob (consumed by the sponsor detail page) ────────
        drug_rows: list[dict] = []
        for d in entry["drug_keys"]:
            summary = drugs_map.get(d) or {}
            drug_rows.append({
                "drug":           d,
                "indication":     (summary.get("indication") or "")[:200],
                "therapy_area":   summary.get("therapy_area", ""),
                "latest_rec":     summary.get("recommendation", ""),
                "latest_year":    summary.get("latest_year", ""),
                "subs_by_this_sponsor": entry["drug_sub_counts"].get(d, 0),
                "spend_aud":      per_drug_spend.get(d, 0),
                "owned":          d in owned,
            })
        # Sort: owned-with-spend first (by spend), then by sponsor's own submission count
        drug_rows.sort(key=lambda r: (
            -(r["spend_aud"] or 0),
            -(r["subs_by_this_sponsor"] or 0),
            r["drug"],
        ))

        # Therapy histogram, sorted by count
        therapy_list = sorted(
            ({"area": a, "n": n} for a, n in entry["therapy_areas"].items()),
            key=lambda r: r["n"], reverse=True,
        )

        # Year activity — keep as {year: count} dict for the chart
        by_year_sorted = {str(y): entry["years"][y] for y in sorted(entry["years"])}

        # Top rejection reasons across this sponsor's not-rec subs.
        # Reasons are comma-separated phrases in the raw field; split, lowercase,
        # tally, return top 5 with original casing of the first occurrence.
        reason_counts: dict[str, int] = {}
        reason_display: dict[str, str] = {}
        for blob in entry["rejection_reasons"]:
            for piece in re.split(r"\s*[,;]\s*", blob):
                p = piece.strip()
                if not p or len(p) < 4:
                    continue
                k = p.lower()
                reason_counts[k] = reason_counts.get(k, 0) + 1
                reason_display.setdefault(k, p)
        top_reasons = sorted(reason_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
        rejection_reasons = [{"reason": reason_display[k], "n": n} for k, n in top_reasons]

        details[key] = {
            "sponsor":           entry["display"],
            "key":               key,
            "n_submissions":     n_subs,
            "n_drugs":           entry["n_drugs"],
            "n_drugs_listed":    entry["n_drugs_listed"],
            "n_recommended":     n_rec,
            "n_not":             entry["n_not"],
            "n_deferred":        entry["n_deferred"],
            "pct_recommended":   pct_rec,
            "total_spend_aud":   total_spend,
            "first_year":        entry["first_year"],
            "last_year":         entry["last_year"],
            "top_therapy_area":  entry["top_therapy_area"],
            "drugs":             drug_rows,
            "therapy_areas":     therapy_list,
            "by_year":           by_year_sorted,
            "rejection_reasons": rejection_reasons,
        }

    leaderboard.sort(key=lambda r: (r["total_spend_aud"], r["n_submissions"]), reverse=True)
    psd["sponsors"] = leaderboard
    psd["sponsor_details"] = details
    psd.pop("sponsors_raw", None)

    n_with_spend = sum(1 for r in leaderboard if r["total_spend_aud"] > 0)
    total_spend = sum(r["total_spend_aud"] for r in leaderboard)
    print(f"  Sponsor leaderboard: {len(leaderboard)} entries, {n_with_spend} with linked spend, "
          f"total ${total_spend/1e9:.2f}B captured")
    print(f"  Sponsor detail blobs: {len(details)} (drugs portfolios + therapy histograms + rejection patterns)")


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


# ── 7. Upcoming PBAC + Intracycle agendas ────────────────────────────────────

def load_pbac_agendas() -> dict:
    """Load data/pbac_agendas.json (produced by agenda_extractor.py).

    Filters to meetings whose date is in the future. Past agendas are kept in
    the source JSON (so re-runs are cheap) but aren't surfaced — once a meeting
    happens its decisions flow through the PSD pipeline instead.
    """
    path = data_path("pbac_agendas.json")
    empty = {"meetings": [], "n_meetings": 0, "n_items": 0, "last_updated": None}
    if not path.exists():
        print("  pbac_agendas.json not found — run `python -m script_report agendas`")
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"  Could not parse pbac_agendas.json ({e})")
        return empty

    # The agenda extractor stores meeting_date as YYYY-MM-01 (month-level
    # precision — the exact day comes from the PBS cycle calendar elsewhere).
    # Compare on YYYY-MM only so a meeting *later this month* isn't filtered
    # out just because today is past the 1st.
    today_ym = date.today().isoformat()[:7]
    upcoming: list[dict] = []
    for ag in data.get("agendas", []) or []:
        mdate = ag.get("meeting_date")
        # Keep undated agendas (rare; defensive) so they're not silently dropped.
        if mdate and mdate[:7] < today_ym:
            continue
        upcoming.append({
            "meeting_label": ag.get("meeting_label"),
            "meeting_date":  mdate,
            "meeting_kind":  ag.get("meeting_kind"),
            "source_url":    ag.get("source_url"),
            "pdf_url":       ag.get("pdf_url"),
            "items":         list(ag.get("items") or []),
        })

    upcoming.sort(key=lambda m: (m.get("meeting_date") or "9999"))
    n_items = sum(len(m["items"]) for m in upcoming)
    print(f"  PBAC agendas: {len(upcoming)} upcoming meetings, {n_items} items")

    return {
        "meetings":     upcoming,
        "n_meetings":   len(upcoming),
        "n_items":      n_items,
        "last_updated": data.get("last_updated"),
    }


def attach_agenda_drug_slugs(agendas: dict, psd: dict) -> None:
    """For each agenda item, fill in `drug_slugs` listing matching drug keys
    in the dashboard's drugs DB so the UI can render backlinks. Works in
    place; safe to call with empty inputs."""
    if not agendas or not psd:
        return
    drugs_map = psd.get("drugs") or {}
    if not drugs_map:
        return
    matched = 0
    for meeting in agendas.get("meetings") or []:
        for item in meeting.get("items") or []:
            slugs: list[str] = []
            for cand in candidate_keys(item.get("drug") or ""):
                if cand in drugs_map and cand not in slugs:
                    slugs.append(cand)
            if slugs:
                item["drug_slugs"] = slugs
                matched += 1
    if matched:
        print(f"  Linked {matched} agenda items to existing drug pages")


# ── 8. PBAC outcomes (post-meeting summaries, pre-PSD) ───────────────────────

def load_pbac_outcomes(psd: dict | None = None) -> dict:
    """Load data/pbac_outcomes.json (produced by outcomes_extractor.py).

    Each meeting holds the rows from the "Recommendations made by the PBAC"
    summary PDF. These bridge the 6–8 week gap between a PBAC meeting and the
    full PSDs landing.

    Per-item retire: if the same drug already has a PSD-extracted record
    matching this meeting's (year, month), drop that item — the full PSD
    carries richer detail elsewhere in the dashboard. Once every item in a
    meeting is retired, the meeting drops out entirely.

    Also enriches each item with `drug_slug` if the drug exists in the
    dashboard's drugs DB (for backlinking the UI).
    """
    path = data_path("pbac_outcomes.json")
    empty = {"meetings": [], "n_meetings": 0, "n_items": 0, "last_updated": None}
    if not path.exists():
        print("  pbac_outcomes.json not found — run `python -m script_report outcomes`")
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"  Could not parse pbac_outcomes.json ({e})")
        return empty

    drugs_map = (psd or {}).get("drugs") or {}

    # Build a lookup: drug_key -> set of (year, month) tuples that already have a PSD.
    drug_psd_months: dict[str, set[tuple[int, int]]] = {}
    for drug_key, summary in drugs_map.items():
        months: set[tuple[int, int]] = set()
        for h in summary.get("history") or []:
            try:
                yr = int(h.get("year") or 0)
                mo = int(h.get("month") or 0)
            except (TypeError, ValueError):
                continue
            if yr and mo:
                months.add((yr, mo))
        if months:
            drug_psd_months[drug_key] = months

    out_meetings: list[dict] = []
    retired = 0
    linked = 0
    for m in data.get("meetings", []) or []:
        mdate = m.get("meeting_date") or ""
        try:
            myr = int(mdate[:4])
            mmo = int(mdate[5:7])
        except (ValueError, TypeError):
            myr = mmo = 0

        kept: list[dict] = []
        for item in m.get("items") or []:
            slugs: list[str] = []
            for cand in candidate_keys(item.get("drug") or ""):
                if cand in drugs_map and cand not in slugs:
                    slugs.append(cand)

            # Retire if any matching drug already has a PSD from this meeting.
            already_psd = False
            if myr and mmo:
                for s in slugs:
                    if (myr, mmo) in drug_psd_months.get(s, set()):
                        already_psd = True
                        break
            if already_psd:
                retired += 1
                continue

            new_item = dict(item)
            if slugs:
                new_item["drug_slugs"] = slugs
                linked += 1
            kept.append(new_item)

        if not kept:
            continue

        # Per-meeting outcome tally for the panel header
        tally: dict[str, int] = {}
        for it in kept:
            o = (it.get("outcome") or "").strip() or "Unknown"
            tally[o] = tally.get(o, 0) + 1

        out_meetings.append({
            "meeting_label": m.get("meeting_label"),
            "meeting_date":  mdate or None,
            "meeting_kind":  m.get("meeting_kind"),
            "source_url":    m.get("source_url"),
            "pdf_url":       m.get("pdf_url"),
            "items":         kept,
            "outcome_tally": tally,
            "n_items":       len(kept),
        })

    # Most-recent meeting first (newest decisions on top).
    out_meetings.sort(key=lambda r: (r.get("meeting_date") or ""), reverse=True)
    n_items = sum(m["n_items"] for m in out_meetings)
    print(f"  PBAC outcomes: {len(out_meetings)} meetings, {n_items} items "
          f"({retired} retired by matching PSDs, {linked} drug backlinks)")

    return {
        "meetings":     out_meetings,
        "n_meetings":   len(out_meetings),
        "n_items":      n_items,
        "last_updated": data.get("last_updated"),
    }
