#!/usr/bin/env python3
"""
Unit tests for _budget_core.py.

Strategy:
- Each test sets BUDGET_PROJECTS_DIR / BUDGET_CALIBRATION_FILE / threshold env vars
  in a TemporaryDirectory, then importlib.reload(_budget_core) so the module-level
  constants pick the new values up.
- Synthesise minimal JSONL fixtures that mirror Claude Code's session log shape:
  one assistant message per line, each with `timestamp` and `message.usage`.
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


def reload_core(env_overrides):
    """Reload _budget_core with given env vars set, others stripped."""
    keys = [
        "BUDGET_PROJECTS_DIR",
        "BUDGET_CALIBRATION_FILE",
        "BUDGET_CALIBRATED_LIMIT",
        "BUDGET_SYNC_PCT",
        "BUDGET_PAUSE_PCT",
        "BUDGET_PAUSE_MODE",
        "BUDGET_RECHECK_SECS",
        "BUDGET_RESET_GRACE_SECS",
        "BUDGET_MAX_SLEEP_SECS",
        "BUDGET_EWMA_ALPHA",
    ]
    for k in keys:
        os.environ.pop(k, None)
    for k, v in env_overrides.items():
        os.environ[k] = str(v)

    if "_budget_core" in sys.modules:
        return importlib.reload(sys.modules["_budget_core"])
    import _budget_core
    return _budget_core


def write_jsonl(path, entries):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    # Make sure mtime is fresh enough to pass the cutoff filter.
    os.utime(path, None)


def usage_entry(ts, *, input_=0, output=0, cache_create=0, cache_read=0):
    if isinstance(ts, (int, float)):
        ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "timestamp": ts,
        "message": {
            "usage": {
                "input_tokens": input_,
                "output_tokens": output,
                "cache_creation_input_tokens": cache_create,
                "cache_read_input_tokens": cache_read,
            }
        },
    }


def bridge_status_entry(ts, content="/remote-control is active"):
    if isinstance(ts, (int, float)):
        ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "timestamp": ts,
        "type": "system",
        "subtype": "bridge_status",
        "content": content,
    }


def api_error_entry(ts, *, status=429, inner_type="rate_limit_error", message="Rate limit exceeded"):
    """Mirror the shape Claude Code records for an Anthropic API error."""
    if isinstance(ts, (int, float)):
        ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "timestamp": ts,
        "type": "system",
        "subtype": "api_error",
        "error": {
            "status": status,
            "error": {
                "type": inner_type,
                "error": {"type": inner_type, "message": message},
            },
            "headers": {},
            "requestID": "req_test",
        },
    }


class LoadEnvFileTests(unittest.TestCase):
    def test_loads_quoted_and_unquoted(self):
        with TemporaryDirectory() as tmp:
            envfile = os.path.join(tmp, ".env")
            with open(envfile, "w") as f:
                f.write("# a comment\n")
                f.write("\n")
                f.write("FOO=bar\n")
                f.write("BAZ=\"quoted value\"\n")
                f.write("QUX='single quoted'\n")
                f.write("MALFORMED_NO_EQUALS\n")

            for k in ("FOO", "BAZ", "QUX", "MALFORMED_NO_EQUALS"):
                os.environ.pop(k, None)

            core = reload_core({"BUDGET_PROJECTS_DIR": tmp})
            core._load_env_file(envfile)

            self.assertEqual(os.environ["FOO"], "bar")
            self.assertEqual(os.environ["BAZ"], "quoted value")
            self.assertEqual(os.environ["QUX"], "single quoted")
            self.assertNotIn("MALFORMED_NO_EQUALS", os.environ)

            for k in ("FOO", "BAZ", "QUX"):
                os.environ.pop(k, None)

    def test_existing_env_wins(self):
        with TemporaryDirectory() as tmp:
            envfile = os.path.join(tmp, ".env")
            with open(envfile, "w") as f:
                f.write("OVERRIDE_ME=from_file\n")

            os.environ["OVERRIDE_ME"] = "from_process"
            core = reload_core({"BUDGET_PROJECTS_DIR": tmp})
            core._load_env_file(envfile)
            self.assertEqual(os.environ["OVERRIDE_ME"], "from_process")
            os.environ.pop("OVERRIDE_ME", None)

    def test_missing_file_silent(self):
        core = reload_core({})
        core._load_env_file("/no/such/file/at/all")  # must not raise


class ParseTsTests(unittest.TestCase):
    def setUp(self):
        self.core = reload_core({})

    def test_iso_with_z(self):
        ts = self.core.parse_ts("2026-05-09T12:00:00Z")
        expect = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(ts, expect, places=3)

    def test_unix_seconds_float(self):
        self.assertAlmostEqual(self.core.parse_ts(1234567890.5), 1234567890.5)

    def test_unix_millis(self):
        self.assertAlmostEqual(self.core.parse_ts(1_700_000_000_000), 1_700_000_000.0)

    def test_falsy(self):
        self.assertEqual(self.core.parse_ts(None), 0.0)
        self.assertEqual(self.core.parse_ts(""), 0.0)
        self.assertEqual(self.core.parse_ts(0), 0.0)

    def test_garbage_string(self):
        self.assertEqual(self.core.parse_ts("not a date"), 0.0)


class RateLimitSignatureTests(unittest.TestCase):
    """_looks_like_rate_limit() now matches structurally on the parsed dict.

    Match rules: type=system, subtype=api_error, AND either HTTP status 429
    OR a nested error.type containing 'rate_limit' / 'usage_limit'. We do NOT
    look at message content text — historically that produced a self-poisoning
    EWMA loop because user/assistant messages routinely discuss "rate limit"
    as a topic.
    """

    def setUp(self):
        self.core = reload_core({})

    # ---------- positive cases (real signature) ----------

    def test_status_429(self):
        d = api_error_entry("2026-05-09T12:00:00Z", status=429, inner_type="rate_limit_error")
        self.assertTrue(self.core._looks_like_rate_limit(d))

    def test_inner_type_rate_limit_error(self):
        d = api_error_entry("2026-05-09T12:00:00Z", status=200, inner_type="rate_limit_error")
        self.assertTrue(self.core._looks_like_rate_limit(d))

    def test_inner_type_usage_limit_error(self):
        d = api_error_entry("2026-05-09T12:00:00Z", status=200, inner_type="usage_limit_error")
        self.assertTrue(self.core._looks_like_rate_limit(d))

    def test_nested_error_type_walks_through_layers(self):
        d = {
            "type": "system",
            "subtype": "api_error",
            "error": {
                "status": 200,  # not 429
                "error": {
                    "type": "api_error",
                    "error": {"type": "rate_limit_error", "message": "..."},  # 3rd level
                },
            },
        }
        self.assertTrue(self.core._looks_like_rate_limit(d))

    # ---------- negative cases (the false-positive class we eliminated) ----------

    def test_assistant_message_with_rate_limit_text(self):
        """User/assistant messages discussing 'rate limit' must not match."""
        for body in [
            "5-hour limit reached",
            "Session limit hit",
            "GitHub API rate limit",
            "rate_limit_error",  # even if the literal type string is in body
        ]:
            d = {
                "type": "assistant",
                "message": {"role": "assistant", "content": body},
            }
            self.assertFalse(self.core._looks_like_rate_limit(d), msg=body)

    def test_user_message_with_rate_limit_text(self):
        d = {
            "type": "user",
            "message": {"role": "user", "content": "Why am I getting limit reached?"},
        }
        self.assertFalse(self.core._looks_like_rate_limit(d))

    def test_other_api_error_does_not_match(self):
        d = api_error_entry("2026-05-09T12:00:00Z", status=401, inner_type="authentication_error")
        self.assertFalse(self.core._looks_like_rate_limit(d))

    def test_system_other_subtype_does_not_match(self):
        d = {"type": "system", "subtype": "bridge_status", "content": "active"}
        self.assertFalse(self.core._looks_like_rate_limit(d))

    def test_non_dict_input_returns_false(self):
        for v in (None, [], "rate_limit_error", 42, ""):
            self.assertFalse(self.core._looks_like_rate_limit(v), msg=repr(v))


class ScanWindowTests(unittest.TestCase):
    def _setup_with_jsonl(self, tmp, entries, *, calib_file=None, **env):
        projects = os.path.join(tmp, "projects")
        os.makedirs(projects, exist_ok=True)
        calib = calib_file or os.path.join(tmp, "calib.json")
        write_jsonl(os.path.join(projects, "p", "session.jsonl"), entries)
        return reload_core({
            "BUDGET_PROJECTS_DIR": projects,
            "BUDGET_CALIBRATION_FILE": calib,
            **{k: str(v) for k, v in env.items()},
        })

    def test_weighted_total_uses_default_weights(self):
        with TemporaryDirectory() as tmp:
            now = time.time()
            core = self._setup_with_jsonl(tmp, [
                usage_entry(now - 60, input_=100, output=10, cache_create=20, cache_read=200),
            ])
            total, oldest, events = core.scan_window(now=now)
            # 100*1 + 10*5 + 20*1.25 + 200*0.10 = 100 + 50 + 25 + 20 = 195
            self.assertEqual(total, 195)
            self.assertEqual(events, [])
            self.assertLess(oldest, now)

    def test_outside_window_excluded(self):
        with TemporaryDirectory() as tmp:
            now = time.time()
            core = self._setup_with_jsonl(tmp, [
                usage_entry(now - (5 * 3600 + 60), input_=1_000_000),  # outside
                usage_entry(now - 60, input_=10),                       # inside
            ])
            total, _, _ = core.scan_window(now=now)
            self.assertEqual(total, 10)

    def test_rate_limit_event_capture(self):
        """A real api_error entry produces exactly one event with the running weighted total."""
        with TemporaryDirectory() as tmp:
            now = time.time()
            projects = os.path.join(tmp, "projects")
            os.makedirs(projects, exist_ok=True)
            entries = [
                usage_entry(now - 120, input_=1000),
                api_error_entry(now - 60, status=429),
            ]
            write_jsonl(os.path.join(projects, "p", "s.jsonl"), entries)
            core = reload_core({
                "BUDGET_PROJECTS_DIR": projects,
                "BUDGET_CALIBRATION_FILE": os.path.join(tmp, "c.json"),
            })
            total, _, events = core.scan_window(now=now)
            self.assertEqual(total, 1000)
            self.assertEqual(len(events), 1)
            ev_ts, weighted_at_event = events[0]
            self.assertAlmostEqual(weighted_at_event, 1000)
            self.assertGreater(ev_ts, now - 121)

    def test_message_body_text_is_not_a_rate_limit_event(self):
        """Regression guard: user/assistant message text mentioning 'rate limit' /
        'limit reached' must NOT register as a rate-limit event. This is the
        false-positive class that previously caused the EWMA self-poisoning loop.
        """
        with TemporaryDirectory() as tmp:
            now = time.time()
            projects = os.path.join(tmp, "projects")
            os.makedirs(projects, exist_ok=True)
            chat_iso = datetime.fromtimestamp(now - 30, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            entries = [
                usage_entry(now - 60, input_=42),
                {  # assistant message body mentioning the topic
                    "timestamp": chat_iso,
                    "type": "assistant",
                    "message": {"role": "assistant", "content": "5-hour limit reached"},
                },
                {  # user message body mentioning the topic
                    "timestamp": chat_iso,
                    "type": "user",
                    "message": {"role": "user", "content": "rate_limit_error"},
                },
            ]
            write_jsonl(os.path.join(projects, "p", "s.jsonl"), entries)
            core = reload_core({
                "BUDGET_PROJECTS_DIR": projects,
                "BUDGET_CALIBRATION_FILE": os.path.join(tmp, "c.json"),
            })
            total, _, events = core.scan_window(now=now)
            self.assertEqual(total, 42)
            self.assertEqual(events, [])  # the key assertion

    def test_malformed_json_skipped(self):
        with TemporaryDirectory() as tmp:
            projects = os.path.join(tmp, "projects")
            os.makedirs(os.path.join(projects, "p"), exist_ok=True)
            now = time.time()
            f = os.path.join(projects, "p", "s.jsonl")
            with open(f, "w") as fh:
                fh.write("not-json garbage line\n")
                fh.write(json.dumps(usage_entry(now - 30, input_=42)) + "\n")
            core = reload_core({
                "BUDGET_PROJECTS_DIR": projects,
                "BUDGET_CALIBRATION_FILE": os.path.join(tmp, "c.json"),
            })
            total, _, _ = core.scan_window(now=now)
            self.assertEqual(total, 42)

    def test_stale_file_skipped_by_mtime(self):
        with TemporaryDirectory() as tmp:
            projects = os.path.join(tmp, "projects")
            os.makedirs(os.path.join(projects, "p"), exist_ok=True)
            now = time.time()
            f = os.path.join(projects, "p", "s.jsonl")
            # In-window content but the file's mtime is stale, so the optimisation
            # short-circuits the scan. This documents the behaviour.
            write_jsonl(f, [usage_entry(now - 60, input_=999)])
            stale = now - (5 * 3600 + 60)
            os.utime(f, (stale, stale))
            core = reload_core({
                "BUDGET_PROJECTS_DIR": projects,
                "BUDGET_CALIBRATION_FILE": os.path.join(tmp, "c.json"),
            })
            total, _, _ = core.scan_window(now=now)
            self.assertEqual(total, 0)


class CalibrationTests(unittest.TestCase):
    def test_load_save_roundtrip(self):
        with TemporaryDirectory() as tmp:
            cf = os.path.join(tmp, "calib.json")
            core = reload_core({
                "BUDGET_PROJECTS_DIR": tmp,
                "BUDGET_CALIBRATION_FILE": cf,
            })
            self.assertEqual(core.load_calibration(), {})
            core.save_calibration({"limit": 12345, "history": []})
            self.assertEqual(core.load_calibration()["limit"], 12345)

    def test_get_calibrated_limit_priority(self):
        with TemporaryDirectory() as tmp:
            cf = os.path.join(tmp, "c.json")
            with open(cf, "w") as f:
                json.dump({"limit": 50_000_000}, f)

            core = reload_core({
                "BUDGET_PROJECTS_DIR": tmp,
                "BUDGET_CALIBRATION_FILE": cf,
            })
            self.assertEqual(core.get_calibrated_limit(), 50_000_000)

            os.environ["BUDGET_CALIBRATED_LIMIT"] = "99"
            self.assertEqual(core.get_calibrated_limit(), 99)
            os.environ.pop("BUDGET_CALIBRATED_LIMIT", None)

    def test_default_limit_when_no_calibration(self):
        with TemporaryDirectory() as tmp:
            core = reload_core({
                "BUDGET_PROJECTS_DIR": tmp,
                "BUDGET_CALIBRATION_FILE": os.path.join(tmp, "missing.json"),
            })
            self.assertEqual(core.get_calibrated_limit(), core.DEFAULT_LIMIT)

    def test_get_weights_overrides_defaults(self):
        with TemporaryDirectory() as tmp:
            cf = os.path.join(tmp, "c.json")
            with open(cf, "w") as f:
                json.dump({"weights": {"input_tokens": 2.0, "bogus_key": 9}}, f)

            core = reload_core({
                "BUDGET_PROJECTS_DIR": tmp,
                "BUDGET_CALIBRATION_FILE": cf,
            })
            w = core.get_weights()
            self.assertEqual(w["input_tokens"], 2.0)
            self.assertEqual(w["output_tokens"], 5.0)  # untouched default
            self.assertNotIn("bogus_key", w)

    def test_save_leaves_no_temp_files_on_success(self):
        """Atomic write pattern: after a successful save, the calibration
        directory contains only the final file — no .budget_cal_*.tmp
        leftovers from tempfile.mkstemp()."""
        with TemporaryDirectory() as tmp:
            cf = os.path.join(tmp, "calib.json")
            core = reload_core({
                "BUDGET_PROJECTS_DIR": tmp,
                "BUDGET_CALIBRATION_FILE": cf,
            })
            core.save_calibration({"limit": 99_999_999})
            self.assertEqual(sorted(os.listdir(tmp)), ["calib.json"])
            with open(cf) as f:
                self.assertEqual(json.load(f)["limit"], 99_999_999)

    def test_save_preserves_existing_on_serialization_failure(self):
        """If json.dump raises mid-write (here: a non-serializable value),
        the original calibration file must remain intact — no truncation,
        no half-written state. This is the core atomic guarantee that the
        prior open(..., 'w') + json.dump pattern did not provide."""
        with TemporaryDirectory() as tmp:
            cf = os.path.join(tmp, "calib.json")
            core = reload_core({
                "BUDGET_PROJECTS_DIR": tmp,
                "BUDGET_CALIBRATION_FILE": cf,
            })
            core.save_calibration({"limit": 12345, "history": []})
            with open(cf) as f:
                before = f.read()

            class NotJsonSerializable:
                pass

            # save_calibration swallows the inner exception by design;
            # the assertion is that the on-disk file was not corrupted.
            core.save_calibration({"limit": NotJsonSerializable()})

            with open(cf) as f:
                self.assertEqual(f.read(), before)
            self.assertEqual(sorted(os.listdir(tmp)), ["calib.json"])


class RecordObservedPctTests(unittest.TestCase):
    def test_seeds_when_no_prior(self):
        with TemporaryDirectory() as tmp:
            projects = os.path.join(tmp, "p")
            os.makedirs(projects, exist_ok=True)
            now = time.time()
            write_jsonl(os.path.join(projects, "x", "s.jsonl"), [
                usage_entry(now - 60, input_=1000, output=100),  # weight: 1000+500 = 1500
            ])
            cf = os.path.join(tmp, "c.json")
            core = reload_core({
                "BUDGET_PROJECTS_DIR": projects,
                "BUDGET_CALIBRATION_FILE": cf,
                "BUDGET_EWMA_ALPHA": "0.3",
            })
            new_limit = core.record_observed_pct(50)  # weighted=1500 → observed_limit=3000
            self.assertEqual(new_limit, 3000)
            self.assertEqual(core.load_calibration()["limit"], 3000)

    def test_ewma_merges_with_prior(self):
        with TemporaryDirectory() as tmp:
            projects = os.path.join(tmp, "p")
            os.makedirs(projects, exist_ok=True)
            now = time.time()
            write_jsonl(os.path.join(projects, "x", "s.jsonl"), [
                usage_entry(now - 60, input_=2000),  # weight=2000
            ])
            cf = os.path.join(tmp, "c.json")
            with open(cf, "w") as f:
                json.dump({"limit": 10_000}, f)
            core = reload_core({
                "BUDGET_PROJECTS_DIR": projects,
                "BUDGET_CALIBRATION_FILE": cf,
                "BUDGET_EWMA_ALPHA": "0.5",
            })
            # observed_pct=50 → observed_limit=4000.
            # EWMA: 0.5*4000 + 0.5*10000 = 7000
            new_limit = core.record_observed_pct(50)
            self.assertEqual(new_limit, 7000)

    def test_invalid_inputs_returns_none(self):
        with TemporaryDirectory() as tmp:
            projects = os.path.join(tmp, "p")
            os.makedirs(projects, exist_ok=True)
            cf = os.path.join(tmp, "c.json")
            core = reload_core({
                "BUDGET_PROJECTS_DIR": projects,
                "BUDGET_CALIBRATION_FILE": cf,
            })
            self.assertIsNone(core.record_observed_pct(50))           # no usage
            self.assertIsNone(core.record_observed_pct(0, weighted=1))


class MaybeUpdateCalibrationTests(unittest.TestCase):
    def _setup(self, tmp, entries, **env):
        projects = os.path.join(tmp, "p")
        os.makedirs(projects, exist_ok=True)
        write_jsonl(os.path.join(projects, "x", "s.jsonl"), entries)
        cf = os.path.join(tmp, "c.json")
        return reload_core({
            "BUDGET_PROJECTS_DIR": projects,
            "BUDGET_CALIBRATION_FILE": cf,
            **{k: str(v) for k, v in env.items()},
        }), cf

    def test_no_events_returns_default(self):
        with TemporaryDirectory() as tmp:
            now = time.time()
            core, _ = self._setup(tmp, [usage_entry(now - 60, input_=1000)])
            self.assertEqual(core.maybe_update_calibration(), core.DEFAULT_LIMIT)

    def test_event_triggers_ewma_update(self):
        with TemporaryDirectory() as tmp:
            now = time.time()
            entries = [
                usage_entry(now - 120, input_=10_000),    # weight=10000
                api_error_entry(now - 60, status=429),    # real rate-limit signature
            ]
            core, cf = self._setup(tmp, entries, BUDGET_EWMA_ALPHA=0.5)
            # prior = DEFAULT_LIMIT (no stored calib), observed = 10_000
            new_limit = core.maybe_update_calibration()
            expected = int(0.5 * 10_000 + 0.5 * core.DEFAULT_LIMIT)
            self.assertEqual(new_limit, expected)
            with open(cf) as fh:
                cal = json.load(fh)
            self.assertEqual(cal["limit"], expected)
            self.assertEqual(len(cal["history"]), 1)
            self.assertEqual(cal["history"][0]["kind"], "rate_limit_detected")

    def test_repeated_event_is_idempotent(self):
        with TemporaryDirectory() as tmp:
            now = time.time()
            entries = [
                usage_entry(now - 120, input_=10_000),
                api_error_entry(now - 60, status=429),
            ]
            core, cf = self._setup(tmp, entries, BUDGET_EWMA_ALPHA=0.5)
            first = core.maybe_update_calibration()
            second = core.maybe_update_calibration()
            self.assertEqual(first, second)
            with open(cf) as fh:
                cal = json.load(fh)
            self.assertEqual(len(cal["history"]), 1)  # not duplicated


class FindSessionAnchorTests(unittest.TestCase):
    def _setup(self, tmp, entries):
        projects = os.path.join(tmp, "p")
        os.makedirs(projects, exist_ok=True)
        write_jsonl(os.path.join(projects, "x", "s.jsonl"), entries)
        return reload_core({
            "BUDGET_PROJECTS_DIR": projects,
            "BUDGET_CALIBRATION_FILE": os.path.join(tmp, "c.json"),
        })

    def test_returns_none_when_no_bridge_status(self):
        with TemporaryDirectory() as tmp:
            now = time.time()
            core = self._setup(tmp, [usage_entry(now - 60, input_=100)])
            self.assertIsNone(core.find_session_anchor(now=now))

    def test_returns_latest_in_window(self):
        with TemporaryDirectory() as tmp:
            now = time.time()
            core = self._setup(tmp, [
                bridge_status_entry(now - 3000),
                bridge_status_entry(now - 600),    # latest — should win
                bridge_status_entry(now - 1800),
            ])
            anchor = core.find_session_anchor(now=now)
            self.assertIsNotNone(anchor)
            self.assertAlmostEqual(anchor, now - 600, delta=1)

    def test_ignores_out_of_window(self):
        with TemporaryDirectory() as tmp:
            now = time.time()
            core = self._setup(tmp, [
                bridge_status_entry(now - (5 * 3600 + 200)),  # outside window
                usage_entry(now - 60, input_=100),            # keeps mtime fresh
            ])
            self.assertIsNone(core.find_session_anchor(now=now))


class ScanWindowAnchorTests(unittest.TestCase):
    def _setup(self, tmp, entries):
        projects = os.path.join(tmp, "p")
        os.makedirs(projects, exist_ok=True)
        write_jsonl(os.path.join(projects, "x", "s.jsonl"), entries)
        return reload_core({
            "BUDGET_PROJECTS_DIR": projects,
            "BUDGET_CALIBRATION_FILE": os.path.join(tmp, "c.json"),
        })

    def test_anchor_used_as_cutoff(self):
        """Messages before the bridge_status anchor must be excluded."""
        with TemporaryDirectory() as tmp:
            now = time.time()
            core = self._setup(tmp, [
                usage_entry(now - 7200, input_=100_000),   # pre-anchor: ignored
                bridge_status_entry(now - 3600),           # anchor
                usage_entry(now - 1800, input_=10),        # post-anchor: counted
            ])
            total, oldest, _ = core.scan_window(now=now)
            self.assertEqual(total, 10)
            self.assertAlmostEqual(oldest, now - 3600, delta=1)

    def test_no_anchor_falls_back_to_window(self):
        """Without bridge_status, behavior matches the old rolling-window logic."""
        with TemporaryDirectory() as tmp:
            now = time.time()
            core = self._setup(tmp, [
                usage_entry(now - 7200, input_=100),
                usage_entry(now - 1800, input_=10),
            ])
            total, oldest, _ = core.scan_window(now=now)
            self.assertEqual(total, 110)
            # oldest should be the earliest in-window usage msg
            self.assertAlmostEqual(oldest, now - 7200, delta=1)

    def test_oldest_returns_anchor_even_if_no_post_anchor_usage(self):
        """If anchor is set but no usage messages after it, oldest = anchor."""
        with TemporaryDirectory() as tmp:
            now = time.time()
            core = self._setup(tmp, [
                usage_entry(now - 7200, input_=500),       # pre-anchor: ignored
                bridge_status_entry(now - 60),
            ])
            total, oldest, _ = core.scan_window(now=now)
            self.assertEqual(total, 0)
            self.assertAlmostEqual(oldest, now - 60, delta=1)


class ThresholdConstantsTests(unittest.TestCase):
    def test_thresholds_picked_from_env(self):
        core = reload_core({"BUDGET_SYNC_PCT": "70", "BUDGET_PAUSE_PCT": "95"})
        self.assertAlmostEqual(core.THRESHOLD_SYNC, 0.70)
        self.assertAlmostEqual(core.THRESHOLD_PAUSE, 0.95)

    def test_sleep_mode_defaults_to_block(self):
        core = reload_core({})
        self.assertEqual(core.HOOK_PAUSE_MODE, "block")
        self.assertEqual(core.HOOK_RECHECK_SECS, 60)
        self.assertEqual(core.HOOK_RESET_GRACE_SECS, 60)
        self.assertEqual(core.HOOK_MAX_SLEEP_SECS, 14400)

    def test_empty_sleep_mode_falls_back_to_block(self):
        core = reload_core({"BUDGET_PAUSE_MODE": ""})
        self.assertEqual(core.HOOK_PAUSE_MODE, "block")

    def test_sleep_mode_picked_from_env(self):
        core = reload_core({
            "BUDGET_PAUSE_MODE": "sleep",
            "BUDGET_RECHECK_SECS": "5",
            "BUDGET_RESET_GRACE_SECS": "7",
            "BUDGET_MAX_SLEEP_SECS": "11",
        })
        self.assertEqual(core.HOOK_PAUSE_MODE, "sleep")
        self.assertEqual(core.HOOK_RECHECK_SECS, 5)
        self.assertEqual(core.HOOK_RESET_GRACE_SECS, 7)
        self.assertEqual(core.HOOK_MAX_SLEEP_SECS, 11)

    def test_invalid_sleep_mode_config_falls_back(self):
        core = reload_core({
            "BUDGET_PAUSE_MODE": "forever",
            "BUDGET_RECHECK_SECS": "0",
            "BUDGET_RESET_GRACE_SECS": "-1",
            "BUDGET_MAX_SLEEP_SECS": "not-a-number",
        })
        self.assertEqual(core.HOOK_PAUSE_MODE, "block")
        self.assertEqual(core.HOOK_RECHECK_SECS, 60)
        self.assertEqual(core.HOOK_RESET_GRACE_SECS, 60)
        self.assertEqual(core.HOOK_MAX_SLEEP_SECS, 14400)

    def test_alpha_negative_falls_back_to_default(self):
        core = reload_core({"BUDGET_EWMA_ALPHA": "-0.5"})
        self.assertAlmostEqual(core.EWMA_ALPHA, 0.3)

    def test_alpha_above_one_falls_back_to_default(self):
        core = reload_core({"BUDGET_EWMA_ALPHA": "1.5"})
        self.assertAlmostEqual(core.EWMA_ALPHA, 0.3)

    def test_alpha_garbage_falls_back_to_default(self):
        core = reload_core({"BUDGET_EWMA_ALPHA": "not-a-number"})
        self.assertAlmostEqual(core.EWMA_ALPHA, 0.3)

    def test_alpha_boundary_values_accepted(self):
        for raw, expected in (("0", 0.0), ("1", 1.0), ("0.05", 0.05)):
            core = reload_core({"BUDGET_EWMA_ALPHA": raw})
            self.assertAlmostEqual(core.EWMA_ALPHA, expected)

    def test_pct_zero_falls_back_to_default(self):
        core = reload_core({"BUDGET_SYNC_PCT": "0", "BUDGET_PAUSE_PCT": "0"})
        self.assertAlmostEqual(core.THRESHOLD_SYNC, 0.80)
        self.assertAlmostEqual(core.THRESHOLD_PAUSE, 0.93)

    def test_pct_negative_falls_back_to_default(self):
        core = reload_core({"BUDGET_SYNC_PCT": "-10", "BUDGET_PAUSE_PCT": "-5"})
        self.assertAlmostEqual(core.THRESHOLD_SYNC, 0.80)
        self.assertAlmostEqual(core.THRESHOLD_PAUSE, 0.93)

    def test_pct_above_100_falls_back_to_default(self):
        core = reload_core({"BUDGET_SYNC_PCT": "150", "BUDGET_PAUSE_PCT": "200"})
        self.assertAlmostEqual(core.THRESHOLD_SYNC, 0.80)
        self.assertAlmostEqual(core.THRESHOLD_PAUSE, 0.93)

    def test_sync_above_pause_resets_both_to_defaults(self):
        # Logical inversion (sync threshold higher than pause threshold) is
        # nonsensical: sync would never trigger before pause already blocked.
        # Reset both to defaults rather than persist the broken ordering.
        core = reload_core({"BUDGET_SYNC_PCT": "95", "BUDGET_PAUSE_PCT": "70"})
        self.assertAlmostEqual(core.THRESHOLD_SYNC, 0.80)
        self.assertAlmostEqual(core.THRESHOLD_PAUSE, 0.93)

    def test_sync_equal_pause_allowed(self):
        core = reload_core({"BUDGET_SYNC_PCT": "85", "BUDGET_PAUSE_PCT": "85"})
        self.assertAlmostEqual(core.THRESHOLD_SYNC, 0.85)
        self.assertAlmostEqual(core.THRESHOLD_PAUSE, 0.85)


if __name__ == "__main__":
    unittest.main(verbosity=2)
