"""
script_report.refresh
━━━━━━━━━━━━━━━━━━━━━
Runs the full script.report data pipeline in one command.

Usage:
    python -m script_report refresh                # full refresh
    python -m script_report refresh --build-only   # skip downloads/extract/embed, rebuild site_data.js
    python -m script_report refresh --no-psds      # skip PSD downloading
    python -m script_report refresh --no-embed     # skip Voyage embedding
    python -m script_report refresh --no-deploy    # build but skip Vercel push

Steps:
    1. Download any new PBAC PSDs from pbs.gov.au
    2. Extract structured data from new PSDs via Claude Haiku (--resume)
    3. Fetch latest PBS drug spend Excel
    4. Embed new PSDs via Voyage AI (--resume)
    5. Build site_data.js
    6. Deploy to Vercel (vercel --prod)

Unlike the legacy refresh.py at the repo root, each step is a direct function
call rather than a subprocess — faster, easier to interrupt, and stack traces
are useful.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import traceback

from script_report.config import REPO_ROOT


def _run_step(label: str, fn, *, argv: list[str] | None = None) -> bool:
    print(f"\n{'=' * 58}")
    print(f"  STEP: {label}")
    print(f"{'=' * 58}")
    # Each underlying main() reads sys.argv via argparse — rewrite it for the duration
    # of the call so the inner argparse sees its own flags only.
    old_argv = sys.argv
    sys.argv = [label, *(argv or [])]
    try:
        fn()
        print(f"\n  ✓ {label} done")
        return True
    except SystemExit as e:
        ok = (e.code or 0) == 0
        print(f"\n  {'✓' if ok else '✗'} {label} {'done' if ok else f'failed (exit {e.code})'}")
        return ok
    except Exception as e:
        print(f"\n  ✗ {label} raised {type(e).__name__}: {e}")
        traceback.print_exc()
        return False
    finally:
        sys.argv = old_argv


def deploy() -> bool:
    print(f"\n{'=' * 58}")
    print("  STEP: Deploy to Vercel")
    print(f"{'=' * 58}")
    result = subprocess.run(["vercel", "--prod"], cwd=REPO_ROOT, shell=True)
    if result.returncode != 0:
        print("  ✗ Deploy failed — check Vercel CLI is installed (npm i -g vercel)")
        return False
    print("  ✓ Deployed")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="script.report full refresh pipeline")
    parser.add_argument("--build-only", action="store_true", help="Skip downloads/extract/embed, just rebuild site_data.js")
    parser.add_argument("--no-deploy",  action="store_true", help="Build but skip Vercel deployment")
    parser.add_argument("--no-psds",    action="store_true", help="Skip downloading new PSDs")
    parser.add_argument("--no-embed",   action="store_true", help="Skip the Voyage embedding step")
    args = parser.parse_args()

    t0 = time.time()
    steps_ok: list[tuple[str, bool]] = []

    # Lazy imports so --build-only doesn't pay the cost of loading scrapers/embedders.
    if not args.build_only:
        if not args.no_psds:
            from script_report.scrapers.psd_downloader import main as psds_main
            steps_ok.append(("Download PSDs",
                             _run_step("Download new PBAC PSDs", psds_main)))

        from script_report.extractors.psd_extractor import main as extract_main
        steps_ok.append(("Extract PSDs",
                         _run_step("Extract PSD data via Claude Haiku",
                                   extract_main, argv=["--resume"])))

        from script_report.scrapers.pbs_spend import main as spend_main
        steps_ok.append(("PBS spend",
                         _run_step("Fetch PBS drug spend data", spend_main)))

        from script_report.scrapers.pbs_schedule import main as schedule_main
        steps_ok.append(("PBS schedule (ATC)",
                         _run_step("Backfill ATC codes from PBS Schedule", schedule_main)))

        from script_report.builders.brand_map_builder import main as brandmap_main
        steps_ok.append(("Brand map",
                         _run_step("Build brand->generic map for Smart Search", brandmap_main)))

        from script_report.extractors.agenda_extractor import main as agenda_main
        steps_ok.append(("Agendas",
                         _run_step("Extract upcoming PBAC + Intracycle agendas", agenda_main)))

        if not args.no_embed:
            from script_report.embedders.voyage_embedder import main as embed_main
            steps_ok.append(("Embed",
                             _run_step("Embed PSDs via Voyage AI",
                                       embed_main, argv=["--resume"])))

    from script_report.builders.site_builder import main as build_main
    steps_ok.append(("Build", _run_step("Build site_data.js", build_main)))

    if not args.no_deploy:
        steps_ok.append(("Deploy", deploy()))

    elapsed = round(time.time() - t0)
    print(f"\n{'=' * 58}")
    print(f"  Pipeline complete in {elapsed}s")
    for label, ok in steps_ok:
        icon = "✓" if ok else "✗"
        print(f"    {icon}  {label}")
    print(f"{'=' * 58}\n")

    if not all(ok for _, ok in steps_ok):
        sys.exit(1)


if __name__ == "__main__":
    main()
