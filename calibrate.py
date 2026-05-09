#!/usr/bin/env python3
"""
One-time calibration: correlates local JSONL token sums with a known /usage percentage.

Usage:
    1. Run /usage inside Claude Code, note 'Current session' percentage
    2. python3 calibrate.py --observed-pct 67
    3. export BUDGET_CALIBRATED_LIMIT=<output>
"""
import argparse, glob, json, os, sys, time
from datetime import datetime

W = {"input_tokens":1.0,"output_tokens":5.0,"cache_creation_input_tokens":1.25,"cache_read_input_tokens":0.10}
WINDOW = 5 * 3600
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")

def parse_ts(v):
    if not v: return 0.0
    if isinstance(v, str):
        try: return datetime.fromisoformat(v.replace("Z","+00:00")).timestamp()
        except: return 0.0
    return v/1000 if v>1e10 else float(v)

def compute(cutoff):
    total, count = 0, 0
    for f in glob.glob(f"{PROJECTS_DIR}/**/*.jsonl", recursive=True):
        try:
            if os.path.getmtime(f) < cutoff: continue
            for line in open(f, errors="ignore"):
                try:
                    d = json.loads(line)
                    u = d.get("message",{}).get("usage",{})
                    if not u or parse_ts(d.get("timestamp")) < cutoff: continue
                    total += int(sum(u.get(k,0)*w for k,w in W.items()))
                    count += 1
                except: pass
        except: pass
    return total, count

parser = argparse.ArgumentParser()
parser.add_argument("--observed-pct", type=float, required=True)
args = parser.parse_args()

if not 1 <= args.observed_pct <= 100:
    print("ERROR: --observed-pct must be 1–100", file=sys.stderr); sys.exit(1)

observed = args.observed_pct / 100
weighted, count = compute(time.time() - WINDOW)

if count == 0:
    print("ERROR: No usage entries in last 5h. Make sure Claude Code was active.", file=sys.stderr); sys.exit(1)

limit = int(weighted / observed)
print(f"\n  Entries:         {count:,}")
print(f"  Weighted tokens: {weighted:,}")
print(f"  Observed usage:  {args.observed_pct:.0f}%")
print(f"\n  ┌──────────────────────────────────────────┐")
print(f"  │  CALIBRATED_LIMIT = {limit:>21,}  │")
print(f"  └──────────────────────────────────────────┘")
print(f"\n  export BUDGET_CALIBRATED_LIMIT={limit}")
print(f"\nKnown baselines:")
print(f"  Claude Max (5x): ~63,226,913  (measured 2026-05-09)")
print(f"  Claude Pro:      unknown — please contribute!")
