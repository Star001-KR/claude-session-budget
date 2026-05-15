#!/usr/bin/env python3
"""
Claude Code PreToolUse hook: checks 5-hour session usage before every tool call.
Exit 0 = proceed. Exit 2 = block (session near limit).

Config (env, ./.env, or ~/.claude/.env):
  BUDGET_CALIBRATED_LIMIT       override stored auto-learned limit
  BUDGET_SYNC_PCT               default 80
  BUDGET_PAUSE_PCT              default 93
  BUDGET_PAUSE_MODE             block (default) or sleep
  BUDGET_RECHECK_SECS           sleep-mode recheck interval, default 60
  BUDGET_RESET_GRACE_SECS       extra recheck grace before resume, default 60
  BUDGET_MAX_SLEEP_SECS         sleep-mode cap, default 14400
  BUDGET_EWMA_ALPHA             default 0.35
  BUDGET_AUTO_CAL_ENABLED       1 (default) — fire auto_calibrate on milestones
  BUDGET_AUTO_CAL_MILESTONES    "90" — pcts that trigger auto-calibration
  BUDGET_AUTO_CAL_COOLDOWN_SECS 300 — min seconds between auto-calibrations
"""
import os, sys, time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _budget_core import (
    scan_window, maybe_update_calibration, maybe_kick_auto_calibrate,
    WINDOW_SECS, THRESHOLD_SYNC, THRESHOLD_PAUSE, HOOK_PAUSE_MODE,
    HOOK_RECHECK_SECS, HOOK_RESET_GRACE_SECS, HOOK_MAX_SLEEP_SECS,
)


def current_status():
    scan = scan_window()
    limit = maybe_update_calibration(scan_result=scan)
    weighted, oldest, _ = scan
    pct = (weighted / limit) if limit else 0.0
    return limit, weighted, oldest, pct


def main():
    limit, weighted, oldest, pct = current_status()

    print(f"[session-budget] {pct*100:.1f}% used ({weighted:,} / {limit:,})", file=sys.stderr)
    reason = maybe_kick_auto_calibrate(pct, oldest)
    if reason:
        print(
            f"[session-budget] auto-calibration triggered ({reason}, background)",
            file=sys.stderr,
        )

    if pct >= THRESHOLD_PAUSE:
        reset_at = oldest + WINDOW_SECS
        wait_min = max((reset_at - time.time()) / 60, 0)
        when = datetime.fromtimestamp(reset_at).strftime("%H:%M")

        if HOOK_PAUSE_MODE == "sleep":
            started = time.time()
            deadline = started + HOOK_MAX_SLEEP_SECS
            print(
                f"[session-budget] SLEEPING — {pct*100:.0f}% >= {THRESHOLD_PAUSE*100:.0f}%. "
                f"Estimated reset ~{when} (~{wait_min:.0f} min). "
                f"Rechecking every {HOOK_RECHECK_SECS}s; max sleep {HOOK_MAX_SLEEP_SECS}s.",
                file=sys.stderr,
            )

            while pct >= THRESHOLD_PAUSE:
                now = time.time()
                remaining_sleep_budget = deadline - now
                if remaining_sleep_budget <= 0:
                    print(
                        f"[session-budget] BLOCKING — still at {pct*100:.0f}% after "
                        f"sleep-mode cap ({HOOK_MAX_SLEEP_SECS}s)",
                        file=sys.stderr,
                    )
                    sys.exit(2)

                time.sleep(min(HOOK_RECHECK_SECS, remaining_sleep_budget))
                limit, weighted, oldest, pct = current_status()
                reset_at = oldest + WINDOW_SECS
                wait_min = max((reset_at - time.time()) / 60, 0)
                when = datetime.fromtimestamp(reset_at).strftime("%H:%M")
                print(
                    f"[session-budget] recheck: {pct*100:.1f}% used "
                    f"({weighted:,} / {limit:,}); reset ~{when} (~{wait_min:.0f} min)",
                    file=sys.stderr,
                )

            if HOOK_RESET_GRACE_SECS:
                grace_sleep = min(HOOK_RESET_GRACE_SECS, max(deadline - time.time(), 0))
                if grace_sleep:
                    time.sleep(grace_sleep)
                limit, weighted, oldest, pct = current_status()

            if pct < THRESHOLD_PAUSE:
                print(
                    f"[session-budget] resumed — {pct*100:.1f}% < {THRESHOLD_PAUSE*100:.0f}%",
                    file=sys.stderr,
                )
                sys.exit(0)

            print(
                f"[session-budget] BLOCKING — still at {pct*100:.0f}% after grace period",
                file=sys.stderr,
            )
            sys.exit(2)

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


if __name__ == "__main__":
    main()
