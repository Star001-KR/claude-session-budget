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
    THRESHOLD_SYNC, THRESHOLD_PAUSE, WINDOW_SECS,
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

    def _refresh_limit(self):
        if self._explicit_limit is None:
            maybe_update_calibration()

    def _compute(self):
        weighted, _, _ = scan_window()
        limit = self.calibrated_limit
        return min(weighted / limit, 1.0) if limit else 0.0, weighted

    def _estimate_reset(self):
        _, oldest, _ = scan_window()
        return oldest + WINDOW_SECS

    def get_status(self):
        self._refresh_limit()
        pct, weighted = self._compute()
        reset_at = self._estimate_reset()
        remaining = max(reset_at - time.time(), 0)
        hrs, rem = divmod(int(remaining), 3600)
        return {
            "pct": round(pct * 100, 1),
            "weighted_tokens": weighted,
            "calibrated_limit": self.calibrated_limit,
            "reset_at": reset_at,
            "remaining_secs": int(remaining),
            "remaining_str": "already reset" if remaining == 0 else f"{hrs}h {rem//60}m",
        }

    async def check_before_dispatch(self):
        """Returns seconds to wait (0 = proceed immediately)."""
        self._refresh_limit()
        pct, weighted = self._compute()
        limit = self.calibrated_limit
        logger.info(f"[Budget] {pct*100:.1f}% ({weighted:,}/{limit:,})")
        if pct >= THRESHOLD_PAUSE:
            wait = max(self._estimate_reset() - time.time(), 0) + 60
            logger.warning(
                f"[Budget] {pct*100:.0f}% >= {THRESHOLD_PAUSE*100:.0f}% — waiting {wait/60:.1f}min"
            )
            return wait
        return 0.0


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
