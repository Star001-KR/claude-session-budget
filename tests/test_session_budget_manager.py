#!/usr/bin/env python3
"""
Unit tests for session_budget_manager.SessionBudgetManager.

Strategy mirrors test_budget_core: each test sets BUDGET_* env vars inside
a TemporaryDirectory, then reloads _budget_core *and* session_budget_manager
so the manager re-imports the freshly-configured core functions.
"""
import importlib
import json
import os
import sys
import time
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

_ENV_KEYS = [
    "BUDGET_PROJECTS_DIR", "BUDGET_CALIBRATION_FILE", "BUDGET_CALIBRATED_LIMIT",
    "BUDGET_SYNC_PCT", "BUDGET_PAUSE_PCT", "BUDGET_EWMA_ALPHA",
    "BUDGET_AUTO_CAL_ENABLED", "BUDGET_SESSION_ANCHOR", "BUDGET_LOAD_PROJECT_ENV",
    "BUDGET_AUTO_CALIBRATE_RUNNING",
]


def reload_sbm(env_overrides):
    """Reload _budget_core then session_budget_manager with env applied, so
    the manager picks up the freshly-configured core functions.

    BUDGET_AUTO_CAL_ENABLED defaults to "0" here: _snapshot() now kicks
    maybe_kick_auto_calibrate, which would fork a real auto_calibrate.py
    child during an ordinary manager test. The dedicated auto-cal test
    monkeypatches the function instead, so it never needs it enabled."""
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    for k, v in {"BUDGET_AUTO_CAL_ENABLED": "0", **env_overrides}.items():
        os.environ[k] = str(v)
    import _budget_core
    importlib.reload(_budget_core)
    import session_budget_manager as sbm
    importlib.reload(sbm)
    return sbm


def write_usage(projects, ts, input_tokens, request_id):
    """Write a one-line JSONL with a single usage entry under projects/p/."""
    d = os.path.join(projects, "p")
    os.makedirs(d, exist_ok=True)
    iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    with open(os.path.join(d, "s.jsonl"), "w") as f:
        f.write(json.dumps({
            "timestamp": iso,
            "requestId": request_id,
            "message": {"id": request_id, "usage": {"input_tokens": input_tokens}},
        }) + "\n")


class CheckAndStatusTests(unittest.IsolatedAsyncioTestCase):
    """#2 — check_and_status() does ONE JSONL scan and returns
    (wait_secs, status_dict); check_before_dispatch() + get_status()
    scans twice."""

    def _empty(self, tmp):
        projects = os.path.join(tmp, "projects")
        os.makedirs(projects, exist_ok=True)
        return reload_sbm({
            "BUDGET_PROJECTS_DIR": projects,
            "BUDGET_CALIBRATION_FILE": os.path.join(tmp, "c.json"),
        })

    async def test_single_scan_returns_wait_and_status(self):
        with TemporaryDirectory() as tmp:
            sbm = self._empty(tmp)
            calls, real = [], sbm.scan_window

            def counting(*a, **k):
                calls.append(1)
                return real(*a, **k)

            sbm.scan_window = counting
            try:
                wait, status = await sbm.SessionBudgetManager().check_and_status()
            finally:
                sbm.scan_window = real
            self.assertEqual(len(calls), 1)            # ONE scan, not two
            self.assertEqual(wait, 0.0)                # 0 usage → proceed
            self.assertIsInstance(status, dict)
            self.assertEqual(status["pct"], 0.0)

    async def test_separate_calls_scan_twice(self):
        """Documents the redundancy check_and_status removes."""
        with TemporaryDirectory() as tmp:
            sbm = self._empty(tmp)
            calls, real = [], sbm.scan_window

            def counting(*a, **k):
                calls.append(1)
                return real(*a, **k)

            sbm.scan_window = counting
            try:
                mgr = sbm.SessionBudgetManager()
                await mgr.check_before_dispatch()
                mgr.get_status()
            finally:
                sbm.scan_window = real
            self.assertEqual(len(calls), 2)

    async def test_returns_wait_when_over_pause_threshold(self):
        with TemporaryDirectory() as tmp:
            projects = os.path.join(tmp, "projects")
            os.makedirs(projects, exist_ok=True)
            # 40M weighted vs 30M DEFAULT_LIMIT → clamped 100% → past 93% pause
            write_usage(projects, time.time() - 600, 40_000_000, "big")
            sbm = reload_sbm({
                "BUDGET_PROJECTS_DIR": projects,
                "BUDGET_CALIBRATION_FILE": os.path.join(tmp, "c.json"),
            })
            wait, status = await sbm.SessionBudgetManager().check_and_status()
            self.assertGreater(wait, 0)
            self.assertGreaterEqual(status["pct"], 93.0)


class ZeroUsageStatusTests(unittest.TestCase):
    """#4 — get_status() reset/remaining handling at zero usage:
      A. no anchor          → reset_at None, remaining_str "n/a"
      B. valid anchor       → reset_at kept (real window end)
      C. expired anchor/gap → reset_at None, remaining_str "n/a"
    """

    def _status(self, tmp, cal_data=None):
        projects = os.path.join(tmp, "projects")
        os.makedirs(projects, exist_ok=True)
        cf = os.path.join(tmp, "c.json")
        if cal_data is not None:
            with open(cf, "w") as f:
                json.dump(cal_data, f)
        sbm = reload_sbm({
            "BUDGET_PROJECTS_DIR": projects,
            "BUDGET_CALIBRATION_FILE": cf,
        })
        return sbm.SessionBudgetManager().get_status()

    def test_case_a_no_anchor(self):
        with TemporaryDirectory() as tmp:
            s = self._status(tmp)
            self.assertEqual(s["pct"], 0.0)
            self.assertIsNone(s["reset_at"])
            self.assertIsNone(s["remaining_secs"])
            self.assertEqual(s["remaining_str"], "n/a")

    def test_case_b_valid_anchor_keeps_reset(self):
        with TemporaryDirectory() as tmp:
            now = time.time()
            ws, we = now - 3600, now + 4 * 3600       # now inside [ws, we]
            s = self._status(tmp, cal_data={"session_window": {
                "window_start": ws, "window_end": we, "anchored_at": ws}})
            self.assertEqual(s["pct"], 0.0)
            self.assertIsNotNone(s["reset_at"])
            self.assertAlmostEqual(s["reset_at"], we, delta=2)
            self.assertNotEqual(s["remaining_str"], "n/a")

    def test_case_c_expired_anchor(self):
        with TemporaryDirectory() as tmp:
            now = time.time()
            ws, we = now - 7 * 3600, now - 2 * 3600   # expired (we < now)
            s = self._status(tmp, cal_data={"session_window": {
                "window_start": ws, "window_end": we, "anchored_at": ws}})
            self.assertEqual(s["pct"], 0.0)
            self.assertIsNone(s["reset_at"])
            self.assertEqual(s["remaining_str"], "n/a")


class SnapshotAutoCalibrateTests(unittest.TestCase):
    """#2 — _snapshot() kicks maybe_kick_auto_calibrate so a headless
    SessionBudgetManager caller self-calibrates without the PreToolUse
    hook. Skipped when the caller pins an explicit limit (it then owns
    calibration). maybe_kick_auto_calibrate is monkeypatched so no real
    auto_calibrate.py child is forked."""

    def _empty(self, tmp):
        projects = os.path.join(tmp, "projects")
        os.makedirs(projects, exist_ok=True)
        return reload_sbm({
            "BUDGET_PROJECTS_DIR": projects,
            "BUDGET_CALIBRATION_FILE": os.path.join(tmp, "c.json"),
        })

    def test_snapshot_kicks_auto_calibrate(self):
        with TemporaryDirectory() as tmp:
            sbm = self._empty(tmp)
            calls, real = [], sbm.maybe_kick_auto_calibrate

            def recording(pct, oldest):
                calls.append((pct, oldest))
                return None

            sbm.maybe_kick_auto_calibrate = recording
            try:
                sbm.SessionBudgetManager().get_status()
            finally:
                sbm.maybe_kick_auto_calibrate = real
            self.assertEqual(len(calls), 1)        # one snapshot → one kick

    def test_explicit_limit_skips_auto_calibrate(self):
        """An explicit calibrated_limit means the caller owns calibration —
        _snapshot must leave auto-cal alone (mirrors maybe_update_calibration)."""
        with TemporaryDirectory() as tmp:
            sbm = self._empty(tmp)
            calls, real = [], sbm.maybe_kick_auto_calibrate
            sbm.maybe_kick_auto_calibrate = lambda *a, **k: calls.append(1)
            try:
                sbm.SessionBudgetManager(calibrated_limit=30_000_000).get_status()
            finally:
                sbm.maybe_kick_auto_calibrate = real
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
