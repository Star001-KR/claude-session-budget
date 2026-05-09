#!/usr/bin/env python3
"""
Claude Code PreToolUse hook: checks 5-hour session usage before every tool call.
Exit 0 = proceed. Exit 2 = block (session near limit).

Config (env, ./.env, or ~/.claude/.env):
  BUDGET_CALIBRATED_LIMIT  override stored auto-learned limit
  BUDGET_SYNC_PCT          default 80
  BUDGET_PAUSE_PCT         default 93
  BUDGET_EWMA_ALPHA        default 0.3
"""
import os, sys, time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _budget_core import (
    scan_window, maybe_update_calibration, WINDOW_SECS,
    THRESHOLD_SYNC, THRESHOLD_PAUSE,
)

limit = maybe_update_calibration()
weighted, oldest, _ = scan_window()
pct = (weighted / limit) if limit else 0.0

print(f"[session-budget] {pct*100:.1f}% used ({weighted:,} / {limit:,})", file=sys.stderr)

if pct >= THRESHOLD_PAUSE:
    reset_at = oldest + WINDOW_SECS
    wait_min = max((reset_at - time.time()) / 60, 0)
    when = datetime.fromtimestamp(reset_at).strftime("%H:%M")
    print(
        f"[session-budget] BLOCKING — {pct*100:.0f}% >= {THRESHOLD_PAUSE*100:.0f}%. "
        f"Resets ~{when} (~{wait_min:.0f} min)",
        file=sys.stderr,
    )
    sys.exit(2)

if pct >= THRESHOLD_SYNC:
    print(
        f"[session-budget] sync threshold passed ({pct*100:.0f}% >= {THRESHOLD_SYNC*100:.0f}%)",
        file=sys.stderr,
    )

sys.exit(0)
