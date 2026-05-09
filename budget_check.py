#!/usr/bin/env python3
\"\"\"
Claude Code PreToolUse hook: checks 5-hour session usage before every tool call.
Exit 0 = proceed. Exit 2 = block (session near limit).

Config via env vars:
  BUDGET_CALIBRATED_LIMIT  (default: 63226913 for Max plan)
  BUDGET_PAUSE_PCT         (default: 93)
\"\"\"
import glob, json, os, sys, time
from datetime import datetime

CALIBRATED_LIMIT = int(os.environ.get("BUDGET_CALIBRATED_LIMIT", 63_226_913))
PAUSE_PCT        = int(os.environ.get("BUDGET_PAUSE_PCT", 93)) / 100
PROJECTS_DIR     = os.environ.get("BUDGET_PROJECTS_DIR", os.path.expanduser("~/.claude/projects"))
W = {"input_tokens": 1.0, "output_tokens": 5.0, "cache_creation_input_tokens": 1.25, "cache_read_input_tokens": 0.10}
WINDOW = 5 * 3600

def parse_ts(v):
    if not v: return 0.0
    if isinstance(v, str):
        try: return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except: return 0.0
    return v / 1000 if v > 1e10 else float(v)

def compute():
    cutoff, total = time.time() - WINDOW, 0
    for f in glob.glob(f"{PROJECTS_DIR}/**/*.jsonl", recursive=True):
        try:
            if os.path.getmtime(f) < cutoff: continue
            for line in open(f, errors="ignore"):
                try:
                    d = json.loads(line)
                    u = d.get("message", {}).get("usage", {})
                    if not u or parse_ts(d.get("timestamp")) < cutoff: continue
                    total += int(sum(u.get(k, 0) * w for k, w in W.items()))
                except: pass
        except: pass
    return total

def estimate_reset():
    cutoff, oldest = time.time() - WINDOW, time.time()
    for f in glob.glob(f"{PROJECTS_DIR}/**/*.jsonl", recursive=True):
        try:
            if os.path.getmtime(f) < cutoff: continue
            for line in open(f, errors="ignore"):
                try:
                    d = json.loads(line)
                    if d.get("message", {}).get("usage"):
                        ts = parse_ts(d.get("timestamp"))
                        if cutoff < ts < oldest: oldest = ts
                except: pass
        except: pass
    reset_at = oldest + WINDOW
    wait = max((reset_at - time.time()) / 60, 0)
    return f"Resets ~{datetime.fromtimestamp(reset_at).strftime('%H:%M')} (~{wait:.0f} min)"

weighted = compute()
pct = weighted / CALIBRATED_LIMIT
print(f"[session-budget] {pct*100:.1f}% used ({weighted:,} / {CALIBRATED_LIMIT:,})", file=sys.stderr)

if pct >= PAUSE_PCT:
    print(f"[session-budget] BLOCKING — {pct*100:.0f}% >= {PAUSE_PCT*100:.0f}%. {estimate_reset()}", file=sys.stderr)
    sys.exit(2)
sys.exit(0)
