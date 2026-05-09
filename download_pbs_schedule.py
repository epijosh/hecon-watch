"""
download_pbs_schedule.py
━━━━━━━━━━━━━━━━━━━━━━━━
Downloads the current PBS Schedule via the public PBS API and saves
it as a CSV you can open in Excel or load into the dashboard/database.

The PBS XML format was discontinued 1 May 2026. This script uses the
replacement public API (no registration required).

API docs: https://data.pbs.gov.au/document/91345.html
Developer portal: https://data-api-portal.health.gov.au/

WHAT YOU GET:
  pbs_schedule_YYYY-MM.csv   — full schedule for the current month
  pbs_schedule_YYYY-MM.json  — same data, JSON format

KEY FIELDS:
  item_code, drug_name, brand_name, form_strength, manufacturer,
  listed_date, restriction_type, authority_type,
  max_quantity, repeats, price_aemp, price_dpmq

RATE LIMIT: public API allows 1 request per 20 seconds (shared).
            This script respects that with a 22 second delay.

REQUIREMENTS:
  pip install requests

USAGE:
  python download_pbs_schedule.py
"""

from __future__ import annotations

import csv
import json
import sys
import time
import logging
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("Run:  pip install requests")
    sys.exit(1)

# ── config ────────────────────────────────────────────────────────────────────
OUTPUT_DIR = Path(__file__).parent
TODAY      = datetime.now().strftime("%Y-%m")
DELAY      = 22   # seconds — public API: 1 req/20s shared; we give a buffer

# PBS Public API base — no auth key required
# Docs: https://data-api-portal.health.gov.au/
API_BASE = "https://data-api.health.gov.au/pbs/api/v3"

HEADERS = {
    "User-Agent": "PBAC-OpenData/1.0 (research; contact via github)",
    "Accept":     "application/json",
}

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

session = requests.Session()
session.headers.update(HEADERS)


def api_get(endpoint: str, params: dict | None = None) -> dict | list | None:
    """Make one API call with rate-limit delay."""
    url = f"{API_BASE}/{endpoint.lstrip('/')}"
    log.info(f"  GET {url}  params={params or {}}")
    time.sleep(DELAY)
    try:
        r = session.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        log.error(f"  Failed: {e}")
        return None


def paginate(endpoint: str, page_size: int = 200) -> list[dict]:
    """
    Fetch all pages from a paginated endpoint.
    Tries common pagination patterns (offset/limit, page/pageSize, etc.)
    """
    results = []
    offset  = 0

    while True:
        data = api_get(endpoint, params={"offset": offset, "limit": page_size})

        if data is None:
            log.warning(f"  Got None — trying without pagination params")
            data = api_get(endpoint)
            if data is None:
                break
            # If unpaginated, just return whatever we got
            if isinstance(data, list):
                results.extend(data)
            elif isinstance(data, dict):
                # look for a data key
                for key in ("data", "items", "results", "records"):
                    if key in data and isinstance(data[key], list):
                        results.extend(data[key])
                        break
                else:
                    results.append(data)
            break

        if isinstance(data, list):
            if not data:
                break
            results.extend(data)
            if len(data) < page_size:
                break  # last page
            offset += page_size

        elif isinstance(data, dict):
            # look for the actual records
            records = None
            for key in ("data", "items", "results", "records"):
                if key in data and isinstance(data[key], list):
                    records = data[key]
                    break
            if records is None:
                results.append(data)
                break
            if not records:
                break
            results.extend(records)
            if len(records) < page_size:
                break
            offset += page_size
        else:
            break

    return results


def flatten(record: dict, prefix: str = "") -> dict:
    """Recursively flatten a nested dict for CSV output."""
    out = {}
    for k, v in record.items():
        key = f"{prefix}{k}" if not prefix else f"{prefix}_{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key))
        elif isinstance(v, list):
            out[key] = "; ".join(str(x) for x in v)
        else:
            out[key] = v
    return out


def try_item_overview() -> list[dict]:
    """
    The item-overview endpoint returns joined drug/item/restriction data.
    This is the richest single endpoint.
    """
    log.info("Trying endpoint: item-overview")
    return paginate("item-overview")


def try_drugs_and_items() -> list[dict]:
    """Fallback: fetch drugs and items separately and merge."""
    log.info("Trying endpoint: drugs")
    drugs = paginate("drugs")
    log.info("Trying endpoint: items")
    items = paginate("items")

    # Build a drug lookup
    drug_map = {}
    for d in drugs:
        did = d.get("drug_id") or d.get("id") or d.get("drug_code")
        if did:
            drug_map[str(did)] = d

    # Merge items with drug data
    merged = []
    for item in items:
        did = item.get("drug_id") or item.get("drug_code")
        if did and str(did) in drug_map:
            combined = {**drug_map[str(did)], **item}
        else:
            combined = item
        merged.append(combined)

    return merged


def save_results(records: list[dict], label: str):
    """Save records as both CSV and JSON."""
    if not records:
        log.warning(f"No records to save for {label}")
        return

    flat = [flatten(r) for r in records]

    # Collect all keys
    all_keys = []
    seen_keys = set()
    for row in flat:
        for k in row:
            if k not in seen_keys:
                all_keys.append(k)
                seen_keys.add(k)

    # CSV
    csv_path = OUTPUT_DIR / f"pbs_schedule_{TODAY}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(flat)
    log.info(f"Saved CSV: {csv_path}  ({len(flat):,} rows)")

    # JSON
    json_path = OUTPUT_DIR / f"pbs_schedule_{TODAY}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, default=str)
    log.info(f"Saved JSON: {json_path}")


def probe_api() -> bool:
    """Quick check that the API is reachable and responding."""
    log.info("Probing PBS API...")
    # The Swagger/OpenAPI spec often lives here
    for probe_url in [
        f"{API_BASE}/item-overview?limit=1",
        f"{API_BASE}/drugs?limit=1",
        f"{API_BASE}/items?limit=1",
    ]:
        time.sleep(DELAY)
        try:
            r = session.get(probe_url, timeout=15)
            if r.status_code == 200:
                log.info(f"  ✓ API reachable via {probe_url}")
                return True
            elif r.status_code == 401:
                log.warning("  API requires authentication — see note below")
                return False
        except requests.RequestException:
            pass
    return False


def main():
    log.info("=" * 60)
    log.info("PBS Schedule Downloader  —  using PBS Public API v3")
    log.info(f"Schedule month: {TODAY}")
    log.info("=" * 60)

    # Check output files don't already exist
    csv_out = OUTPUT_DIR / f"pbs_schedule_{TODAY}.csv"
    if csv_out.exists():
        log.info(f"Already have {csv_out.name} — delete it to re-download")
        return

    reachable = probe_api()

    if not reachable:
        log.warning("")
        log.warning("━━━  PBS API NOTE  ━━━")
        log.warning("The public PBS API may now require a (free) subscription key.")
        log.warning("Register at: https://data-api-portal.health.gov.au/")
        log.warning("")
        log.warning("ALTERNATIVE — manual CSV download (always works):")
        log.warning("  1. Go to https://www.pbs.gov.au/browse/downloads")
        log.warning("  2. Download 'PBS API CSV files' for the current month")
        log.warning("  3. Save to this folder")
        log.warning("")
        log.warning("Once you have a subscription key, add it to this script:")
        log.warning('  session.headers["Ocp-Apim-Subscription-Key"] = "YOUR_KEY"')
        return

    # Try richest endpoint first, fall back to separate endpoints
    records = try_item_overview()

    if not records:
        log.info("item-overview returned no data — trying drugs + items separately")
        records = try_drugs_and_items()

    if not records:
        log.error("Could not retrieve any schedule data.")
        log.error("Check https://data-api-portal.health.gov.au/ for current endpoints.")
        return

    log.info(f"Total records retrieved: {len(records):,}")
    save_results(records, TODAY)

    log.info("")
    log.info("Done. Next steps:")
    log.info("  1. Open pbs_schedule_*.csv in Excel to explore")
    log.info("  2. Run generate_psd_data.py to refresh the dashboard")


if __name__ == "__main__":
    main()
