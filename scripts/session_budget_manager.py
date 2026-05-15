#!/usr/bin/env python3
"""
SessionBudgetManager — async class for PM layer / orchestrator integration.
Tracks Claude Code 5-hour session usage via local JSONL files.

Usage:
    from session_budget_manager import SessionBudgetManager
    budget = SessionBudgetManager()
    wait = await budget.check_before_dispatch()
    if wait:
        await asyncio.sleep(wait)
"""
import asyncio, logging, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _budget_core import (
    scan_window, maybe_update_calibration, get_calibrated_limit,
    get_session_anchor, THRESHOLD_SYNC, THRESHOLD_PAUSE, WINDOW_SECS,
)

logger = logging.getLogger(__name__)


class SessionBudgetManager:
    def __init__(self, calibrated_limit=None):
        self._explicit_limit = calibrated_limit

    @property
    def calibrated_limit(self):
        if self._explicit_limit is not None:
            return self._explicit_limit
        return get_calibrated_limit()

    def _snapshot(self):
        """Single JSONL scan; refresh calibration off the same result."""
        scan = scan_window()
        if self._explicit_limit is None:
            maybe_update_calibration(scan_result=scan)
        weighted, oldest, _ = scan
        limit = self.calibrated_limit
        pct = min(weighted / limit, 1.0) if limit else 0.0
        return pct, weighted, oldest, limit

    def _status_dict(self, snap):
        """Build the status dict from a snapshot tuple.

        #4: with zero usage, scan_window's `oldest` falls back to `now`, so
        a naive `reset_at = oldest + 5h` would report a meaningless
        "resets in ~5h" at 0% usage. That fallback is only legitimate when
        `now` is inside a valid /usage session anchor (Case B — `oldest`
        is the real window_start). With no anchor (A) or an expired one
        (C) there is no running session: `reset_at`/`remaining_secs` are
        None and `remaining_str` is "n/a".
        """
        pct, weighted, oldest, limit = snap
        reset_at = oldest + WINDOW_SECS
        if weighted == 0:
            now = time.time()
            anchor = get_session_anchor()
            if not (anchor is not None and anchor[0] <= now <= anchor[1]):
                reset_at = None
        if reset_at is None:
            remaining_secs, remaining_str = None, "n/a"
        else:
            remaining = max(reset_at - time.time(), 0)
            hrs, rem = divmod(int(remaining), 3600)
            remaining_secs = int(remaining)
            remaining_str = "already reset" if remaining == 0 else f"{hrs}h {rem//60}m"
        return {
            "pct": round(pct * 100, 1),
            "weighted_tokens": weighted,
            "calibrated_limit": limit,
            "reset_at": reset_at,
            "remaining_secs": remaining_secs,
            "remaining_str": remaining_str,
        }

    def get_status(self):
        """Status snapshot dict. `reset_at`/`remaining_secs` are None and
        `remaining_str` is "n/a" when there is no running session (0 usage
        with no active anchor) — see _status_dict."""
        return self._status_dict(self._snapshot())

    async def check_before_dispatch(self):
        """Returns seconds to wait (0 = proceed immediately)."""
        pct, weighted, oldest, limit = self._snapshot()
        logger.info(f"[Budget] {pct*100:.1f}% ({weighted:,}/{limit:,})")
        if pct >= THRESHOLD_PAUSE:
            wait = max(oldest + WINDOW_SECS - time.time(), 0) + 60
            logger.warning(
                f"[Budget] {pct*100:.0f}% >= {THRESHOLD_PAUSE*100:.0f}% — waiting {wait/60:.1f}min"
            )
            return wait
        return 0.0

    async def check_and_status(self):
        """One JSONL scan → (wait_secs, status_dict).

        Use this when one dispatch cycle needs both the pause decision and
        the status text: calling check_before_dispatch() then get_status()
        scans the JSONL twice (and the two scans can disagree if usage
        lands between them). This shares a single _snapshot().
        """
        snap = self._snapshot()
        pct, weighted, oldest, limit = snap
        logger.info(f"[Budget] {pct*100:.1f}% ({weighted:,}/{limit:,})")
        wait = 0.0
        if pct >= THRESHOLD_PAUSE:
            wait = max(oldest + WINDOW_SECS - time.time(), 0) + 60
            logger.warning(
                f"[Budget] {pct*100:.0f}% >= {THRESHOLD_PAUSE*100:.0f}% — waiting {wait/60:.1f}min"
            )
        return wait, self._status_dict(snap)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    async def main():
        mgr = SessionBudgetManager()
        s = mgr.get_status()
        print(
            f"Session usage: {s['pct']}%  "
            f"({s['weighted_tokens']:,} / {s['calibrated_limit']:,} weighted tokens)"
        )
        wait = await mgr.check_before_dispatch()
        print("WAIT" if wait else "OK", f"— {wait/60:.1f}min" if wait else "proceed")

    asyncio.run(main())
