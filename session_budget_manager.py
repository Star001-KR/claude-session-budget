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
import asyncio, glob, json, logging, os, re, time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

CALIBRATED_LIMIT = int(os.environ.get("BUDGET_CALIBRATED_LIMIT", 63_226_913))
W = {"input_tokens":1.0,"output_tokens":5.0,"cache_creation_input_tokens":1.25,"cache_read_input_tokens":0.10}
THRESHOLD_SYNC  = float(os.environ.get("BUDGET_SYNC_PCT",  "80")) / 100
THRESHOLD_PAUSE = float(os.environ.get("BUDGET_PAUSE_PCT", "93")) / 100
WINDOW_SECS  = 5 * 3600
PROJECTS_DIR = os.environ.get("BUDGET_PROJECTS_DIR", os.path.expanduser("~/.claude/projects"))

def _parse_ts(v):
    if not v: return 0.0
    if isinstance(v, str):
        try: return datetime.fromisoformat(v.replace("Z","+00:00")).timestamp()
        except: return 0.0
    return v/1000 if v>1e10 else float(v)

class SessionBudgetManager:
    def __init__(self, calibrated_limit=CALIBRATED_LIMIT):
        self.calibrated_limit = calibrated_limit

    def _compute(self):
        cutoff, total, count = time.time()-WINDOW_SECS, 0, 0
        for f in glob.glob(f"{PROJECTS_DIR}/**/*.jsonl", recursive=True):
            try:
                if os.path.getmtime(f) < cutoff: continue
                for line in open(f, errors="ignore"):
                    try:
                        d = json.loads(line)
                        u = d.get("message",{}).get("usage",{})
                        if not u or _parse_ts(d.get("timestamp")) < cutoff: continue
                        total += int(sum(u.get(k,0)*w for k,w in W.items()))
                        count += 1
                    except: pass
            except: pass
        logger.debug(f"[Budget] {count} entries, weighted={total:,}")
        return min(total/self.calibrated_limit, 1.0), total

    def _estimate_reset(self):
        cutoff, oldest = time.time()-WINDOW_SECS, time.time()
        for f in glob.glob(f"{PROJECTS_DIR}/**/*.jsonl", recursive=True):
            try:
                if os.path.getmtime(f) < cutoff: continue
                for line in open(f, errors="ignore"):
                    try:
                        d = json.loads(line)
                        if d.get("message",{}).get("usage"):
                            ts = _parse_ts(d.get("timestamp"))
                            if cutoff < ts < oldest: oldest = ts
                    except: pass
            except: pass
        return oldest + WINDOW_SECS

    def get_status(self):
        pct, weighted = self._compute()
        return {"pct": round(pct*100,1), "weighted_tokens": weighted, "calibrated_limit": self.calibrated_limit}

    async def check_before_dispatch(self):
        """Returns seconds to wait (0 = proceed immediately)."""
        pct, weighted = self._compute()
        logger.info(f"[Budget] {pct*100:.1f}% ({weighted:,}/{self.calibrated_limit:,})")
        if pct >= THRESHOLD_PAUSE:
            reset_at = self._estimate_reset()
            wait = max(reset_at - time.time(), 0) + 60
            logger.warning(f"[Budget] {pct*100:.0f}% >= {THRESHOLD_PAUSE*100:.0f}% — waiting {wait/60:.1f}min")
            return wait
        return 0.0

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    async def main():
        mgr = SessionBudgetManager()
        s = mgr.get_status()
        print(f"Session usage: {s['pct']}%  ({s['weighted_tokens']:,} / {s['calibrated_limit']:,} weighted tokens)")
        wait = await mgr.check_before_dispatch()
        print("WAIT" if wait else "OK", f"— {wait/60:.1f}min" if wait else "proceed")
    asyncio.run(main())
