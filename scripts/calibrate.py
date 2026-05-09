#!/usr/bin/env python3
"""
Manual calibration entry point. Auto-learning from JSONL rate-limit events runs
on every budget_check.py invocation; this script is for periodic /usage readings.

Usage:
    1. Run /usage inside Claude Code, note 'Current session' percentage.
    2. python3 calibrate.py --observed-pct 67

The result is EWMA-merged into ~/.claude/.budget_calibration.json and picked
up automatically on next run; no env var needed.
"""
import argparse, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _budget_core import (
    record_observed_pct, scan_window, load_calibration,
    CALIBRATION_FILE, EWMA_ALPHA,
)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--observed-pct", type=float, required=True, help="Current session %% from /usage (1-100)")
    args = p.parse_args()

    if not 1 <= args.observed_pct <= 100:
        print("ERROR: --observed-pct must be 1–100", file=sys.stderr)
        sys.exit(1)

    weighted, _, _ = scan_window()
    if weighted == 0:
        print("ERROR: No usage entries in last 5h. Make sure Claude Code was active.", file=sys.stderr)
        sys.exit(1)

    prior_stored = load_calibration().get("limit")
    new_limit = record_observed_pct(args.observed_pct, weighted=weighted)

    print(f"\n  Weighted tokens: {weighted:,}")
    print(f"  Observed usage:  {args.observed_pct:.0f}%")
    if isinstance(prior_stored, (int, float)) and prior_stored > 0:
        print(f"  Prior limit:     {int(prior_stored):,}")
        print(f"  New limit (EWMA α={EWMA_ALPHA}): {new_limit:,}")
    else:
        print(f"  Prior limit:     none (first calibration — seeding directly)")
        print(f"  New limit:       {new_limit:,}")
    print(f"\nSaved to {CALIBRATION_FILE}")
    print("Auto-loaded on next run; no env var needed.")
    print("\nKnown baselines:")
    print("  Claude Max (5x): ~63,226,913  (measured 2026-05-09)")
    print("  Claude Pro:      unknown — please contribute!")


if __name__ == "__main__":
    main()
