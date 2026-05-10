"""
refresh.py
━━━━━━━━━━
Runs the full script.report data pipeline in one command.

Usage:
    python refresh.py             # full refresh
    python refresh.py --build-only  # skip downloads, just rebuild site_data.js
    python refresh.py --no-deploy   # build but don't push to Vercel

Steps:
    1. Download any new PBAC PSDs from pbs.gov.au
    2. Extract structured data from new PSDs via Claude Haiku (--resume)
    3. Fetch latest PBS drug spend Excel
    4. Embed new PSDs via Voyage AI (--resume)
    5. Build site_data.js
    6. Deploy to Vercel (vercel --prod)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent


def run(cmd: list[str], label: str) -> bool:
    """Run a command, print output live. Returns True on success."""
    print(f"\n{'='*58}")
    print(f"  STEP: {label}")
    print(f"{'='*58}")
    result = subprocess.run([sys.executable] + cmd, cwd=HERE)
    if result.returncode != 0:
        print(f"\n  ✗ {label} failed (exit {result.returncode})")
        return False
    print(f"\n  ✓ {label} done")
    return True


def deploy() -> bool:
    print(f"\n{'='*58}")
    print("  STEP: Deploy to Vercel")
    print(f"{'='*58}")
    result = subprocess.run(["vercel", "--prod"], cwd=HERE, shell=True)
    if result.returncode != 0:
        print("  ✗ Deploy failed — check Vercel CLI is installed (npm i -g vercel)")
        return False
    print("  ✓ Deployed")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="script.report full refresh pipeline")
    parser.add_argument("--build-only",  action="store_true", help="Skip downloads, just rebuild site_data.js")
    parser.add_argument("--no-deploy",   action="store_true", help="Build but skip Vercel deployment")
    parser.add_argument("--no-psds",     action="store_true", help="Skip downloading new PSDs")
    parser.add_argument("--no-embed",    action="store_true", help="Skip the Voyage embedding step")
    args = parser.parse_args()

    t0 = time.time()
    steps_ok = []

    if not args.build_only:
        # 1. Download new PSDs
        if not args.no_psds:
            ok = run(["download_missing_psds.py"], "Download new PBAC PSDs")
            steps_ok.append(("Download PSDs", ok))

        # 2. Extract new PSDs
        ok = run(["extract_psd_text.py", "--resume"], "Extract PSD data via Claude Haiku")
        steps_ok.append(("Extract PSDs", ok))

        # 3. Fetch PBS drug spend
        ok = run(["fetch_pbs_drug_spend.py"], "Fetch PBS drug spend data")
        steps_ok.append(("PBS spend", ok))

        # 4. Embed PSDs (Voyage) — only re-embeds drugs that aren't yet in psd_embeddings.bin
        if not args.no_embed:
            ok = run(["embed_psds.py", "--resume"], "Embed PSDs via Voyage AI")
            steps_ok.append(("Embed", ok))

    # 6. Build site_data.js
    ok = run(["build_site_data.py"], "Build site_data.js")
    steps_ok.append(("Build", ok))

    # 6. Deploy
    if not args.no_deploy:
        ok = deploy()
        steps_ok.append(("Deploy", ok))

    # Summary
    elapsed = round(time.time() - t0)
    print(f"\n{'='*58}")
    print(f"  Pipeline complete in {elapsed}s")
    for label, ok in steps_ok:
        icon = "✓" if ok else "✗"
        print(f"    {icon}  {label}")
    print(f"{'='*58}\n")

    if not all(ok for _, ok in steps_ok):
        sys.exit(1)


if __name__ == "__main__":
    main()
