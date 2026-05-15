#!/usr/bin/env python3
"""
Unit tests for _budget_core.py.

Strategy:
- Each test sets BUDGET_PROJECTS_DIR / BUDGET_CALIBRATION_FILE / threshold env vars
  in a TemporaryDirectory, then importlib.reload(_budget_core) so the module-level
  constants pick the new values up.
- Synthesise minimal JSONL fixtures that mirror Claude Code's session log shape:
  assistant entries with `timestamp` and `message.usage`. Claude Code emits
  one jsonl line per content block of a turn, all sharing one `requestId`;
  pass `request_id=` to usage_entry() to model that and exercise dedup.
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
        "BUDGET_LOAD_PROJECT_ENV",
        "BUDGET_AUTO_CAL_ENABLED",
        "BUDGET_AUTO_CAL_MILESTONES",
        "BUDGET_AUTO_CAL_COOLDOWN_SECS",
        "BUDGET_AUTO_CALIBRATE_RUNNING",
        "BUDGET_SESSION_ANCHOR",
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


def usage_entry(ts, *, input_=0, output=0, cache_create=0, cache_read=0,
                cache_create_5m=None, cache_create_1h=None,
                request_id=None, message_id=None):
    """Build a synthetic usage entry.

    `cache_create` is the legacy flat field; `cache_create_5m` and
    `cache_create_1h` populate the per-TTL breakdown that newer Claude
    Code builds emit. Pass either form (or both, in which case the
    legacy total should equal the breakdown sum, mirroring real jsonl).

    `request_id` / `message_id` populate the top-level `requestId` and
    `message.id`. Real Claude Code emits one jsonl line per content
    block of an assistant turn, all repeating the same usage and sharing
    one requestId — give several entries the same `request_id` to model
    that and exercise scan_window's de-duplication.
    """
    if isinstance(ts, (int, float)):
        ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    usage = {
        "input_tokens": input_,
        "output_tokens": output,
        "cache_creation_input_tokens": cache_create,
        "cache_read_input_tokens": cache_read,
    }
    if cache_create_5m is not None or cache_create_1h is not None:
        usage["cache_creation"] = {
            "ephemeral_5m_input_tokens": cache_create_5m or 0,
            "ephemeral_1h_input_tokens": cache_create_1h or 0,
        }
    message = {"usage": usage}
    if message_id is not None:
        message["id"] = message_id
    entry = {"timestamp": ts, "message": message}
    if request_id is not None:
        entry["requestId"] = request_id
    return entry


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

    def test_module_import_skips_cwd_env_by_default(self):
        """Without BUDGET_LOAD_PROJECT_ENV, importing _budget_core must not
        load ./.env from the current working directory. Removes the
        import-time cwd side effect and the untrusted-cwd attack surface."""
        with TemporaryDirectory() as tmp:
            envfile = os.path.join(tmp, ".env")
            with open(envfile, "w") as f:
                f.write("CWD_ENV_KEY_DEFAULT_OFF=should_not_load\n")
            os.environ.pop("CWD_ENV_KEY_DEFAULT_OFF", None)
            prev = os.getcwd()
            try:
                os.chdir(tmp)
                reload_core({"BUDGET_PROJECTS_DIR": tmp})
                self.assertNotIn("CWD_ENV_KEY_DEFAULT_OFF", os.environ)
            finally:
                os.chdir(prev)
                os.environ.pop("CWD_ENV_KEY_DEFAULT_OFF", None)

    def test_module_import_loads_cwd_env_when_opted_in(self):
        """With BUDGET_LOAD_PROJECT_ENV=1, importing _budget_core re-enables
        the per-project override path."""
        with TemporaryDirectory() as tmp:
            envfile = os.path.join(tmp, ".env")
            with open(envfile, "w") as f:
                f.write("CWD_ENV_KEY_OPTED_IN=loaded\n")
            os.environ.pop("CWD_ENV_KEY_OPTED_IN", None)
            prev = os.getcwd()
            try:
                os.chdir(tmp)
                reload_core({
                    "BUDGET_PROJECTS_DIR": tmp,
                    "BUDGET_LOAD_PROJECT_ENV": "1",
                })
                self.assertEqual(os.environ.get("CWD_ENV_KEY_OPTED_IN"), "loaded")
            finally:
                os.chdir(prev)
                os.environ.pop("CWD_ENV_KEY_OPTED_IN", None)


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


class ScanWindowDedupTests(unittest.TestCase):
    """scan_window charges each API request once.

    Claude Code writes one jsonl line per content block of an assistant
    turn (thinking, text, each tool_use) and repeats the identical
    message.usage on every line — all sharing one requestId. Summing per
    line multiplies a tool-heavy turn by its block count; scan_window
    must de-duplicate on requestId (message.id as fallback).
    """

    def setUp(self):
        self.now = time.time()

    def _scan_total(self, tmp, entries):
        projects = os.path.join(tmp, "projects")
        os.makedirs(projects, exist_ok=True)
        write_jsonl(os.path.join(projects, "p", "s.jsonl"), entries)
        core = reload_core({
            "BUDGET_PROJECTS_DIR": projects,
            "BUDGET_CALIBRATION_FILE": os.path.join(tmp, "c.json"),
        })
        return core.scan_window(now=self.now)[0]

    def test_content_block_split_counted_once(self):
        """12 lines, one requestId, identical usage — counted once, not 12x."""
        with TemporaryDirectory() as tmp:
            entries = [
                usage_entry(self.now - 60, input_=1000, output=200,
                            request_id="req_A", message_id="msg_A")
                for _ in range(12)
            ]
            # one request: 1000*1 + 200*5 = 2000  (NOT 12 * 2000)
            self.assertEqual(self._scan_total(tmp, entries), 2000)

    def test_distinct_requests_both_counted(self):
        with TemporaryDirectory() as tmp:
            entries = [
                usage_entry(self.now - 90, input_=1000, request_id="req_A"),
                usage_entry(self.now - 80, input_=1000, request_id="req_A"),  # same turn
                usage_entry(self.now - 60, input_=500, request_id="req_B"),
            ]
            self.assertEqual(self._scan_total(tmp, entries), 1500)

    def test_message_id_fallback_when_no_request_id(self):
        """No requestId present → dedup falls back to message.id."""
        with TemporaryDirectory() as tmp:
            entries = [
                usage_entry(self.now - 60, input_=777, message_id="msg_X")
                for _ in range(4)
            ]
            self.assertEqual(self._scan_total(tmp, entries), 777)

    def test_keyless_entries_each_counted(self):
        """Entries with neither requestId nor message.id can't be deduped —
        each counts, preserving behaviour for that (older / synthetic) shape."""
        with TemporaryDirectory() as tmp:
            entries = [
                usage_entry(self.now - 90, input_=100),
                usage_entry(self.now - 60, input_=100),
            ]
            self.assertEqual(self._scan_total(tmp, entries), 200)

    def test_dedup_spans_multiple_files(self):
        """A requestId appearing in more than one file is still one request."""
        with TemporaryDirectory() as tmp:
            projects = os.path.join(tmp, "projects")
            os.makedirs(projects, exist_ok=True)
            write_jsonl(os.path.join(projects, "a", "s.jsonl"),
                        [usage_entry(self.now - 60, input_=1000, request_id="req_A")])
            write_jsonl(os.path.join(projects, "b", "s.jsonl"),
                        [usage_entry(self.now - 50, input_=1000, request_id="req_A")])
            core = reload_core({
                "BUDGET_PROJECTS_DIR": projects,
                "BUDGET_CALIBRATION_FILE": os.path.join(tmp, "c.json"),
            })
            self.assertEqual(core.scan_window(now=self.now)[0], 1000)


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
                json.dump({"limit": 50_000_000, "counts_deduped": True}, f)

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
                json.dump({"limit": 10_000, "counts_deduped": True}, f)
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


class CalibrationMigrationTests(unittest.TestCase):
    """Auto-migration off pre-dedup calibration.

    A calibration file written before the content-block dedup fix holds
    a `limit` derived from ~3.3x-inflated weighted totals and lacks the
    `counts_deduped` marker. get_calibrated_limit must ignore that stale
    limit (fall back to DEFAULT_LIMIT) so the install base migrates on
    upgrade instead of flipping to under-prediction.
    """

    def test_pre_dedup_limit_is_ignored(self):
        with TemporaryDirectory() as tmp:
            cf = os.path.join(tmp, "c.json")
            with open(cf, "w") as f:
                json.dump({"limit": 64_000_000}, f)  # no counts_deduped marker
            core = reload_core({
                "BUDGET_PROJECTS_DIR": tmp,
                "BUDGET_CALIBRATION_FILE": cf,
            })
            self.assertEqual(core.get_calibrated_limit(), core.DEFAULT_LIMIT)

    def test_post_dedup_limit_is_trusted(self):
        with TemporaryDirectory() as tmp:
            cf = os.path.join(tmp, "c.json")
            with open(cf, "w") as f:
                json.dump({"limit": 28_000_000, "counts_deduped": True}, f)
            core = reload_core({
                "BUDGET_PROJECTS_DIR": tmp,
                "BUDGET_CALIBRATION_FILE": cf,
            })
            self.assertEqual(core.get_calibrated_limit(), 28_000_000)

    def test_record_observed_pct_stamps_marker(self):
        """A fresh calibration writes counts_deduped=True so it is trusted
        from then on."""
        with TemporaryDirectory() as tmp:
            projects = os.path.join(tmp, "p")
            os.makedirs(projects, exist_ok=True)
            write_jsonl(os.path.join(projects, "x", "s.jsonl"), [
                usage_entry(time.time() - 60, input_=1000, request_id="req_A"),
            ])
            cf = os.path.join(tmp, "c.json")
            core = reload_core({
                "BUDGET_PROJECTS_DIR": projects,
                "BUDGET_CALIBRATION_FILE": cf,
            })
            core.record_observed_pct(50)
            self.assertTrue(core.load_calibration().get("counts_deduped"))

    def test_stale_prior_not_blended_into_new_calibration(self):
        """record_observed_pct must not EWMA-merge against a pre-dedup
        (inflated) prior limit — it seeds fresh from the observation."""
        with TemporaryDirectory() as tmp:
            projects = os.path.join(tmp, "p")
            os.makedirs(projects, exist_ok=True)
            write_jsonl(os.path.join(projects, "x", "s.jsonl"), [
                usage_entry(time.time() - 60, input_=2000, request_id="req_A"),
            ])
            cf = os.path.join(tmp, "c.json")
            with open(cf, "w") as f:
                json.dump({"limit": 10_000}, f)  # pre-dedup: no marker
            core = reload_core({
                "BUDGET_PROJECTS_DIR": projects,
                "BUDGET_CALIBRATION_FILE": cf,
                "BUDGET_EWMA_ALPHA": "0.5",
            })
            # observed 50% → observed_limit 4000. Stale prior 10000 is
            # ignored → fresh seed 4000, not the EWMA blend (7000).
            self.assertEqual(core.record_observed_pct(50), 4000)


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
            self.assertTrue(cal["counts_deduped"])
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


class ComputeWeightedTests(unittest.TestCase):
    """compute_weighted: TTL-aware cache_creation accounting.

    Anthropic prices 1h-TTL cache writes at 2.0× base input price vs 1.25×
    for the default 5m TTL. The legacy flat `cache_creation_input_tokens`
    field doesn't expose this distinction, so when the per-TTL breakdown
    is present we must consume it (and not double-count by also applying
    the legacy field's weight)."""

    def _core(self):
        return reload_core({})

    def test_basic_input_output_only(self):
        core = self._core()
        u = {"input_tokens": 100, "output_tokens": 10}
        # 100*1 + 10*5 = 150
        self.assertEqual(core.compute_weighted(u), 150)

    def test_legacy_cache_creation_uses_125x(self):
        core = self._core()
        u = {"cache_creation_input_tokens": 1000}
        self.assertEqual(core.compute_weighted(u), 1250)

    def test_ttl_breakdown_5m_uses_125x(self):
        core = self._core()
        u = {"cache_creation": {"ephemeral_5m_input_tokens": 1000,
                                 "ephemeral_1h_input_tokens": 0}}
        self.assertEqual(core.compute_weighted(u), 1250)

    def test_ttl_breakdown_1h_uses_2x(self):
        core = self._core()
        u = {"cache_creation": {"ephemeral_5m_input_tokens": 0,
                                 "ephemeral_1h_input_tokens": 1000}}
        self.assertEqual(core.compute_weighted(u), 2000)

    def test_breakdown_present_supersedes_legacy_field(self):
        """When both are present (real Claude Code behavior), weight only the
        breakdown. Otherwise we'd double-count: legacy (1.25× × 1000) PLUS
        breakdown (2.0× × 1000) = 3250, which is wrong."""
        core = self._core()
        u = {
            "cache_creation_input_tokens": 1000,
            "cache_creation": {"ephemeral_5m_input_tokens": 0,
                               "ephemeral_1h_input_tokens": 1000},
        }
        # Should be 2000 (1h-only), NOT 3250
        self.assertEqual(core.compute_weighted(u), 2000)

    def test_cache_read_uses_010x(self):
        core = self._core()
        u = {"cache_read_input_tokens": 10_000}
        self.assertEqual(core.compute_weighted(u), 1000)

    def test_full_realistic_entry(self):
        """Mirrors a real Claude Code usage entry — input + output + 1h cache
        write + cache read — to verify all four contributions sum correctly."""
        core = self._core()
        u = {
            "input_tokens": 6,
            "output_tokens": 29,
            "cache_read_input_tokens": 18_192,
            "cache_creation_input_tokens": 39_923,
            "cache_creation": {"ephemeral_5m_input_tokens": 0,
                               "ephemeral_1h_input_tokens": 39_923},
        }
        # 6*1 + 29*5 + 18192*0.10 + 39923*2.0
        # = 6 + 145 + 1819.2 + 79846 = 81816.2 → int 81816
        self.assertEqual(core.compute_weighted(u), 81816)

    def test_empty_or_none_returns_zero(self):
        core = self._core()
        self.assertEqual(core.compute_weighted(None), 0)
        self.assertEqual(core.compute_weighted({}), 0)

    def test_partial_breakdown_dict_treated_as_breakdown(self):
        """A breakdown dict missing one TTL key is still TTL-aware — the
        absent key counts as zero, and we must NOT silently fall back to
        the legacy field (which would then double-count when present)."""
        core = self._core()
        u = {
            "cache_creation_input_tokens": 500,
            "cache_creation": {"ephemeral_5m_input_tokens": 500},  # 1h missing
        }
        self.assertEqual(core.compute_weighted(u), 625)  # 500*1.25, no double-count


class ScanWindowAnchorTests(unittest.TestCase):
    """scan_window: bridge_status entries must NOT influence the cutoff.

    Earlier versions treated `type=system, subtype=bridge_status` as a
    "5h Max session start" anchor, but those events fire on every CLI
    attach to /remote-control — multiple times within a single 5h window
    in normal use. Using them as an anchor caused the cutoff to leap
    forward whenever the user opened a new claude CLI, silently zeroing
    the budget mid-window. We now use plain rolling-5h with no anchor
    promotion, and these tests pin the new behaviour as a regression
    guard against re-introducing the anchor."""

    def _setup(self, tmp, entries):
        projects = os.path.join(tmp, "p")
        os.makedirs(projects, exist_ok=True)
        write_jsonl(os.path.join(projects, "x", "s.jsonl"), entries)
        return reload_core({
            "BUDGET_PROJECTS_DIR": projects,
            "BUDGET_CALIBRATION_FILE": os.path.join(tmp, "c.json"),
        })

    def test_bridge_status_does_not_truncate_window(self):
        """Pre-bridge_status usage MUST still be counted — that's the bug fix."""
        with TemporaryDirectory() as tmp:
            now = time.time()
            core = self._setup(tmp, [
                usage_entry(now - 7200, input_=100_000),   # pre-bridge: counted now
                bridge_status_entry(now - 3600),           # ignored as anchor
                usage_entry(now - 1800, input_=10),
            ])
            total, oldest, _ = core.scan_window(now=now)
            self.assertEqual(total, 100_010)               # both usages counted
            self.assertAlmostEqual(oldest, now - 7200, delta=1)

    def test_plain_rolling_window(self):
        """Without bridge_status, rolling-5h: count everything in (-5h, now]."""
        with TemporaryDirectory() as tmp:
            now = time.time()
            core = self._setup(tmp, [
                usage_entry(now - 7200, input_=100),
                usage_entry(now - 1800, input_=10),
            ])
            total, oldest, _ = core.scan_window(now=now)
            self.assertEqual(total, 110)
            self.assertAlmostEqual(oldest, now - 7200, delta=1)

    def test_no_usage_oldest_is_now(self):
        """If no in-window usage exists, oldest defaults to `now` so reset
        time = now + 5h (effectively 'fresh window'). bridge_status alone
        does NOT seed oldest."""
        with TemporaryDirectory() as tmp:
            now = time.time()
            core = self._setup(tmp, [
                usage_entry(now - 7200, input_=500),       # outside-ish but still in 5h window
                bridge_status_entry(now - 60),
            ])
            total, oldest, _ = core.scan_window(now=now)
            # 7200s = 2h, which IS within 5h, so it counts
            self.assertEqual(total, 500)
            self.assertAlmostEqual(oldest, now - 7200, delta=1)

    def test_rate_limit_event_within_window_captured(self):
        """A rate-limit api_error within the rolling 5h must produce an event
        regardless of bridge_status presence."""
        with TemporaryDirectory() as tmp:
            now = time.time()
            core = self._setup(tmp, [
                usage_entry(now - 7200, input_=10_000),
                api_error_entry(now - 5400, status=429),
                bridge_status_entry(now - 3600),           # ignored
                usage_entry(now - 1800, input_=10),
            ])
            total, oldest, events = core.scan_window(now=now)
            self.assertEqual(total, 10_010)
            self.assertAlmostEqual(oldest, now - 7200, delta=1)
            self.assertEqual(len(events), 1)
            ev_ts, weighted_at_event = events[0]
            self.assertAlmostEqual(weighted_at_event, 10_000)
            self.assertAlmostEqual(ev_ts, now - 5400, delta=1)

    def test_future_bridge_status_no_effect(self):
        """A bridge_status with ts > now (clock skew, replayed line) is
        already harmless — bridge_status is no longer used for anchoring,
        so the rolling-5h count proceeds unaffected."""
        with TemporaryDirectory() as tmp:
            now = time.time()
            core = self._setup(tmp, [
                usage_entry(now - 1800, input_=100),
                bridge_status_entry(now + 600),  # 10 min in the future
            ])
            total, oldest, _ = core.scan_window(now=now)
            self.assertEqual(total, 100)
            self.assertAlmostEqual(oldest, now - 1800, delta=1)

    def test_multiple_bridge_status_within_window_no_effect(self):
        """Real failure mode: user opens claude CLI several times within one
        5h window. Each attach writes a bridge_status. The previous
        implementation took max() of these as the cutoff, dropping all
        earlier usage. Pin that this is no longer the case."""
        with TemporaryDirectory() as tmp:
            now = time.time()
            core = self._setup(tmp, [
                usage_entry(now - 14_000, input_=5_000),    # 3.9h ago
                bridge_status_entry(now - 14_000),           # CLI open #1
                usage_entry(now - 7_000, input_=3_000),
                bridge_status_entry(now - 3_600),            # CLI open #2 (would be old anchor)
                usage_entry(now - 1_800, input_=2_000),
                bridge_status_entry(now - 60),               # CLI open #3 (would be newer anchor)
            ])
            total, oldest, _ = core.scan_window(now=now)
            # All three usage entries should count — none dropped by anchor leap.
            self.assertEqual(total, 10_000)
            self.assertAlmostEqual(oldest, now - 14_000, delta=1)


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
        self.assertAlmostEqual(core.EWMA_ALPHA, 0.35)

    def test_alpha_above_one_falls_back_to_default(self):
        core = reload_core({"BUDGET_EWMA_ALPHA": "1.5"})
        self.assertAlmostEqual(core.EWMA_ALPHA, 0.35)

    def test_alpha_garbage_falls_back_to_default(self):
        core = reload_core({"BUDGET_EWMA_ALPHA": "not-a-number"})
        self.assertAlmostEqual(core.EWMA_ALPHA, 0.35)

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


class AutoCalibrateTriggerTests(unittest.TestCase):
    """should_fire_auto_calibrate / mark_milestone_fired: gate the
    background auto_calibrate.py invocation. Each milestone fires AT MOST
    once per 5h session window, with a hard cooldown so transient hook
    bursts don't dispatch multiple workers."""

    def _core_with_cal(self, tmp, env=None):
        env = env or {}
        cf = os.path.join(tmp, "cal.json")
        with open(cf, "w") as f:
            json.dump({"limit": 60_000_000}, f)
        return reload_core({
            "BUDGET_CALIBRATION_FILE": cf,
            "BUDGET_AUTO_CAL_COOLDOWN_SECS": "0",  # disable for unit tests
            **env,
        }), cf

    def test_below_first_milestone_returns_none(self):
        with TemporaryDirectory() as tmp:
            core, _ = self._core_with_cal(tmp)
            now = time.time()
            self.assertIsNone(core.should_fire_auto_calibrate(0.50, now - 1800, now=now))

    def test_first_crossing_returns_lowest_milestone(self):
        with TemporaryDirectory() as tmp:
            core, _ = self._core_with_cal(tmp)
            now = time.time()
            # 90% threshold (default sole milestone)
            self.assertEqual(core.should_fire_auto_calibrate(0.91, now - 1800, now=now), 0.90)

    def test_already_fired_milestone_skipped(self):
        with TemporaryDirectory() as tmp:
            core, cf = self._core_with_cal(tmp)
            now = time.time()
            oldest = now - 1800
            core.mark_milestone_fired(0.90, oldest, now=now)
            # 95% — only one milestone configured and it has fired
            self.assertIsNone(core.should_fire_auto_calibrate(0.95, oldest, now=now))

    def test_progresses_through_milestones(self):
        with TemporaryDirectory() as tmp:
            # Override with multi-milestone env to verify progression semantics
            core, _ = self._core_with_cal(tmp, env={"BUDGET_AUTO_CAL_MILESTONES": "80,90,95"})
            now = time.time()
            oldest = now - 1800
            core.mark_milestone_fired(0.80, oldest, now=now)
            # Now at 91% — next milestone is 90%
            self.assertEqual(core.should_fire_auto_calibrate(0.91, oldest, now=now), 0.90)
            core.mark_milestone_fired(0.90, oldest, now=now)
            # Now at 96% — next is 95%
            self.assertEqual(core.should_fire_auto_calibrate(0.96, oldest, now=now), 0.95)
            core.mark_milestone_fired(0.95, oldest, now=now)
            # All milestones fired in this window — nothing left to fire
            self.assertIsNone(core.should_fire_auto_calibrate(0.99, oldest, now=now))

    def test_new_window_resets_fired_milestones(self):
        with TemporaryDirectory() as tmp:
            core, _ = self._core_with_cal(tmp)
            now = time.time()
            oldest_1 = now - 1800
            core.mark_milestone_fired(0.90, oldest_1, now=now)
            # New window: oldest moves forward by several hours
            oldest_2 = now + 10_000  # any value yielding a different 30-min bucket
            self.assertEqual(
                core.should_fire_auto_calibrate(0.91, oldest_2, now=now), 0.90
            )

    def test_cooldown_blocks_back_to_back_firing(self):
        with TemporaryDirectory() as tmp:
            core, cf = self._core_with_cal(tmp, env={"BUDGET_AUTO_CAL_COOLDOWN_SECS": "300"})
            now = time.time()
            oldest = now - 1800
            # Pretend a dispatch happened 10s ago — child not yet finished
            with open(cf) as fh:
                cal = json.load(fh)
            cal["auto_cal_state"] = {"window_key": int(oldest // 1800),
                                      "fired": [], "last_dispatch_ts": now - 10}
            with open(cf, "w") as fh:
                json.dump(cal, fh)
            self.assertIsNone(core.should_fire_auto_calibrate(0.91, oldest, now=now))

    def test_mark_milestone_records_dispatch_time(self):
        """mark_milestone_fired sets last_dispatch_ts (hook-side cooldown
        anchor), NOT last_success_ts (which is the child's job).
        Regression guard: an earlier version mixed these and the child
        process saw its own dispatch as a cooldown trigger and bailed."""
        with TemporaryDirectory() as tmp:
            core, cf = self._core_with_cal(tmp)
            now = time.time()
            oldest = now - 1800
            core.mark_milestone_fired(0.90, oldest, now=now)
            with open(cf) as fh:
                cal = json.load(fh)
            state = cal["auto_cal_state"]
            self.assertIn("last_dispatch_ts", state)
            self.assertNotIn("last_success_ts", state)
            self.assertAlmostEqual(state["last_dispatch_ts"], now, delta=1)

    def test_recursion_guard_disables_firing(self):
        with TemporaryDirectory() as tmp:
            core, _ = self._core_with_cal(tmp, env={"BUDGET_AUTO_CALIBRATE_RUNNING": "1"})
            now = time.time()
            self.assertIsNone(core.should_fire_auto_calibrate(0.95, now - 1800, now=now))

    def test_disabled_via_env(self):
        with TemporaryDirectory() as tmp:
            core, _ = self._core_with_cal(tmp, env={"BUDGET_AUTO_CAL_ENABLED": "0"})
            now = time.time()
            self.assertIsNone(core.should_fire_auto_calibrate(0.95, now - 1800, now=now))

    def test_custom_milestones_via_env(self):
        with TemporaryDirectory() as tmp:
            core, _ = self._core_with_cal(tmp, env={"BUDGET_AUTO_CAL_MILESTONES": "70,93"})
            now = time.time()
            # 70% should now fire
            self.assertEqual(core.should_fire_auto_calibrate(0.72, now - 1800, now=now), 0.70)

    def test_auto_calibrate_supported_on_posix(self):
        """On POSIX `auto_calibrate_supported()` is True (pty stdlib always
        present). On Windows the answer depends on pywinpty install — see
        `test_auto_calibrate_supported_on_windows_with_winpty`."""
        if sys.platform == "win32":
            self.skipTest("posix-only check")
        with TemporaryDirectory() as tmp:
            core, _ = self._core_with_cal(tmp)
            self.assertTrue(core.auto_calibrate_supported())

    def test_auto_calibrate_supported_on_windows_with_winpty(self):
        """When sys.platform spoofs win32 AND a winpty module is importable,
        the gate must return True. We inject a stub `winpty` module into
        sys.modules to simulate a successful `pip install pywinpty`."""
        with TemporaryDirectory() as tmp:
            core, _ = self._core_with_cal(tmp)
            import types
            saved_platform = sys.platform
            saved_winpty = sys.modules.get("winpty")
            try:
                sys.platform = "win32"
                stub = types.ModuleType("winpty")
                stub.PtyProcess = type("PtyProcess", (), {})
                sys.modules["winpty"] = stub
                self.assertTrue(core.auto_calibrate_supported())
            finally:
                sys.platform = saved_platform
                if saved_winpty is None:
                    sys.modules.pop("winpty", None)
                else:
                    sys.modules["winpty"] = saved_winpty

    def test_auto_calibrate_supported_on_windows_without_winpty(self):
        """When sys.platform spoofs win32 AND winpty is not importable,
        the gate must return False so the hook skips dispatch instead of
        spawning a worker that would crash on `import winpty`."""
        with TemporaryDirectory() as tmp:
            core, _ = self._core_with_cal(tmp)
            saved_platform = sys.platform
            saved_winpty = sys.modules.pop("winpty", None)
            # Block re-import via sys.modules sentinel (None means "not found")
            sys.modules["winpty"] = None  # type: ignore
            try:
                sys.platform = "win32"
                self.assertFalse(core.auto_calibrate_supported())
            finally:
                sys.platform = saved_platform
                sys.modules.pop("winpty", None)
                if saved_winpty is not None:
                    sys.modules["winpty"] = saved_winpty

    def test_unsupported_platform_blocks_firing(self):
        """When auto_calibrate_supported() returns False, no milestone
        ever fires regardless of pct. Simulated here by monkey-patching
        the module-level helper rather than spoofing sys.platform."""
        with TemporaryDirectory() as tmp:
            core, _ = self._core_with_cal(tmp)
            original = core.auto_calibrate_supported
            try:
                core.auto_calibrate_supported = lambda: False
                now = time.time()
                self.assertIsNone(
                    core.should_fire_auto_calibrate(0.95, now - 1800, now=now)
                )
            finally:
                core.auto_calibrate_supported = original


class ParseUsageTextTests(unittest.TestCase):
    """calibrate.parse_usage_text: pull `Current session NN%` from pasted
    /usage panel text. The parser has to survive ANSI escapes (TTY capture),
    Unicode block characters in the bar, and the panel's other percentage
    rows (Current week, Sonnet only) which must NOT be picked up."""

    def _import(self):
        # calibrate is in scripts/ alongside _budget_core; SCRIPTS_DIR is on sys.path
        import importlib
        if "calibrate" in sys.modules:
            return importlib.reload(sys.modules["calibrate"])
        import calibrate
        return calibrate

    def test_real_paste_picks_session_pct(self):
        cal = self._import()
        text = """  Settings  Status   Config   Usage   Stats

  Current session
  ██████████████████████████████████████████████████ 100% used
  Resets 3:40am (Asia/Seoul)

  Current week (all models)
  █████████████████████████████████▌                 67% used
  Resets May 11 at 5am (Asia/Seoul)

  Current week (Sonnet only)
  █                                                  2% used
"""
        pct, reset = cal.parse_usage_text(text)
        self.assertEqual(pct, 100.0)
        self.assertIn("3:40am", reset)

    def test_does_not_match_weekly_row(self):
        """If 'Current session' is missing or out of order, must not pick the
        weekly row by accident. Returns None rather than the wrong number."""
        cal = self._import()
        text = """  Current week (all models)
  ████ 67% used
  Resets May 11 at 5am
"""
        pct, _ = cal.parse_usage_text(text)
        self.assertIsNone(pct)

    def test_session_row_under_heading_wins_over_weekly(self):
        """When both blocks are present, the row right under 'Current session'
        is chosen — never the weekly one even if it appears first in the file."""
        cal = self._import()
        text = """  Current session
  ████ 23% used
  Resets 9:15pm

  Current week (all models)
  ████████ 67% used
"""
        pct, _ = cal.parse_usage_text(text)
        self.assertEqual(pct, 23.0)

    def test_strips_ansi_escapes(self):
        cal = self._import()
        text = "\x1b[2C\x1b[5A  Current session\n  \x1b[33m█\x1b[0m 75% used\n  Resets 9:15pm\n"
        pct, reset = cal.parse_usage_text(text)
        self.assertEqual(pct, 75.0)
        self.assertIn("9:15pm", reset)

    def test_decimal_percentages(self):
        cal = self._import()
        text = "Current session\n  88.5% used\n  Resets 3:40am\n"
        pct, _ = cal.parse_usage_text(text)
        self.assertEqual(pct, 88.5)

    def test_empty_or_garbage_returns_none(self):
        cal = self._import()
        self.assertEqual(cal.parse_usage_text(""), (None, None))
        self.assertEqual(cal.parse_usage_text("hello world"), (None, None))


class ParseResetToTsTests(unittest.TestCase):
    """parse_reset_to_ts: a /usage 'Resets HH(:MM)(am|pm)' clue → the
    Unix timestamp of the next occurrence of that wall-clock time, in
    the machine's local time."""

    def setUp(self):
        self.core = reload_core({})

    def test_midnight_resolves_to_next_local_midnight(self):
        now = datetime(2026, 5, 15, 19, 0, 0).timestamp()
        ts = self.core.parse_reset_to_ts("Resets 12am (Asia/Seoul)", now=now)
        self.assertAlmostEqual(ts, datetime(2026, 5, 16, 0, 0, 0).timestamp(), delta=1)

    def test_pm_time_today_when_still_ahead(self):
        now = datetime(2026, 5, 15, 19, 0, 0).timestamp()
        ts = self.core.parse_reset_to_ts("Resets 11:30pm", now=now)
        self.assertAlmostEqual(ts, datetime(2026, 5, 15, 23, 30, 0).timestamp(), delta=1)

    def test_time_already_passed_rolls_to_tomorrow(self):
        now = datetime(2026, 5, 15, 19, 0, 0).timestamp()
        ts = self.core.parse_reset_to_ts("Resets 3:40am", now=now)
        self.assertAlmostEqual(ts, datetime(2026, 5, 16, 3, 40, 0).timestamp(), delta=1)

    def test_noon(self):
        now = datetime(2026, 5, 15, 6, 0, 0).timestamp()
        ts = self.core.parse_reset_to_ts("Resets 12pm", now=now)
        self.assertAlmostEqual(ts, datetime(2026, 5, 15, 12, 0, 0).timestamp(), delta=1)

    def test_unparseable_returns_none(self):
        self.assertIsNone(self.core.parse_reset_to_ts("Resets soon"))
        self.assertIsNone(self.core.parse_reset_to_ts(""))
        self.assertIsNone(self.core.parse_reset_to_ts(None))


class SessionAnchorTests(unittest.TestCase):
    """session_window_is_valid / save_session_anchor / get_session_anchor:
    a /usage reset time is a trustworthy *current*-session end only when
    it lands in (now, now+5h]."""

    def test_window_end_in_range_is_valid(self):
        core = reload_core({})
        now = time.time()
        self.assertTrue(core.session_window_is_valid(now + 3600, now))

    def test_expired_window_end_is_invalid(self):
        """A lagging /usage still showing the just-expired session → reject."""
        core = reload_core({})
        now = time.time()
        self.assertFalse(core.session_window_is_valid(now - 600, now))

    def test_far_future_window_end_is_invalid(self):
        core = reload_core({})
        now = time.time()
        self.assertFalse(core.session_window_is_valid(now + 6 * 3600, now))

    def test_save_and_get_roundtrip(self):
        with TemporaryDirectory() as tmp:
            cf = os.path.join(tmp, "c.json")
            core = reload_core({"BUDGET_PROJECTS_DIR": tmp,
                                "BUDGET_CALIBRATION_FILE": cf})
            now = time.time()
            we = now + 2 * 3600
            self.assertTrue(core.save_session_anchor(we, now=now))
            anchor = core.get_session_anchor()
            self.assertIsNotNone(anchor)
            ws, got_we = anchor
            self.assertAlmostEqual(got_we, we, delta=1)
            self.assertAlmostEqual(ws, we - 5 * 3600, delta=1)

    def test_save_rejects_invalid_window_end(self):
        with TemporaryDirectory() as tmp:
            cf = os.path.join(tmp, "c.json")
            core = reload_core({"BUDGET_PROJECTS_DIR": tmp,
                                "BUDGET_CALIBRATION_FILE": cf})
            now = time.time()
            self.assertFalse(core.save_session_anchor(now - 600, now=now))
            self.assertIsNone(core.get_session_anchor())


class SessionCutoffTests(unittest.TestCase):
    """scan_window anchors its cutoff to the real 5h session boundary.

    A plain rolling now-5h window straddles a session reset — right after
    a reset it keeps summing the previous, already-expired session. The
    anchor (a /usage "Resets" time) lets scan_window count from the
    session START instead."""

    def _core(self, tmp, entries, cal_data=None, **env):
        projects = os.path.join(tmp, "projects")
        os.makedirs(projects, exist_ok=True)
        write_jsonl(os.path.join(projects, "p", "s.jsonl"), entries)
        cf = os.path.join(tmp, "c.json")
        if cal_data is not None:
            with open(cf, "w") as f:
                json.dump(cal_data, f)
        return reload_core({
            "BUDGET_PROJECTS_DIR": projects,
            "BUDGET_CALIBRATION_FILE": cf,
            **{k: str(v) for k, v in env.items()},
        })

    def test_anchor_excludes_previous_session(self):
        """THE straddle fix: usage before window_start is not counted."""
        with TemporaryDirectory() as tmp:
            now = time.time()
            ws = now - 3600                       # current session started 1h ago
            core = self._core(tmp, [
                usage_entry(now - 9000, input_=1_000_000, request_id="prev"),
                usage_entry(now - 1800, input_=100, request_id="cur"),
            ], cal_data={"session_window": {"window_start": ws,
                                            "window_end": ws + 5 * 3600,
                                            "anchored_at": now}})
            total, oldest, _ = core.scan_window(now=now)
            self.assertEqual(total, 100)          # previous-session 1M excluded
            self.assertAlmostEqual(oldest, ws, delta=1)

    def test_no_anchor_falls_back_to_rolling(self):
        with TemporaryDirectory() as tmp:
            now = time.time()
            core = self._core(tmp, [
                usage_entry(now - 9000, input_=1000, request_id="a"),
                usage_entry(now - 1800, input_=100, request_id="b"),
            ])  # no calibration file → no anchor → rolling-5h
            total, _, _ = core.scan_window(now=now)
            self.assertEqual(total, 1100)

    def test_disabled_via_env_uses_rolling(self):
        with TemporaryDirectory() as tmp:
            now = time.time()
            ws = now - 3600
            core = self._core(tmp, [
                usage_entry(now - 9000, input_=1000, request_id="a"),
                usage_entry(now - 1800, input_=100, request_id="b"),
            ], cal_data={"session_window": {"window_start": ws,
                                            "window_end": ws + 5 * 3600,
                                            "anchored_at": now}},
               BUDGET_SESSION_ANCHOR="0")
            total, _, _ = core.scan_window(now=now)
            self.assertEqual(total, 1100)          # anchor ignored

    def test_rollforward_across_expired_anchor(self):
        """Anchor expired; cutoff rolls to the first activity after it."""
        with TemporaryDirectory() as tmp:
            now = time.time()
            we_old = now - 7200                   # previous session ended 2h ago
            ws_old = we_old - 5 * 3600
            core = self._core(tmp, [
                usage_entry(now - 4 * 3600, input_=1000, request_id="prev"),
                usage_entry(now - 3600, input_=200, request_id="cur"),
            ], cal_data={"session_window": {"window_start": ws_old,
                                            "window_end": we_old,
                                            "anchored_at": ws_old}})
            total, oldest, _ = core.scan_window(now=now)
            self.assertEqual(total, 200)
            self.assertAlmostEqual(oldest, now - 3600, delta=1)

    def test_gap_after_expiry_reports_zero(self):
        """Anchor expired with no activity since → in a gap, 0 usage."""
        with TemporaryDirectory() as tmp:
            now = time.time()
            we_old = now - 3600
            ws_old = we_old - 5 * 3600
            core = self._core(tmp, [
                usage_entry(now - 2 * 3600, input_=5000, request_id="prev"),
            ], cal_data={"session_window": {"window_start": ws_old,
                                            "window_end": we_old,
                                            "anchored_at": ws_old}})
            total, _, _ = core.scan_window(now=now)
            self.assertEqual(total, 0)


    def test_rollforward_does_not_resurrect_expired_session(self):
        """P1 regression: a session whose first request is older than 5h
        (clipped by the rolling scan) must not be resurrected — its
        in-window tail belongs to an already-expired session, so usage
        reads 0 (gap), not the tail."""
        with TemporaryDirectory() as tmp:
            now = time.time()
            we = now - 6 * 3600                  # expired anchor, 6h-old window_end
            ws = we - 5 * 3600
            core = self._core(tmp, [
                usage_entry(now - 5.5 * 3600, input_=1, request_id="first"),
                usage_entry(now - 4 * 3600, input_=999, request_id="tail"),
            ], cal_data={"session_window": {"window_start": ws, "window_end": we,
                                            "anchored_at": ws}})
            total, _, _ = core.scan_window(now=now)
            self.assertEqual(total, 0)           # session [-5.5h,-0.5h] expired → gap

    def test_anchor_beyond_lookback_falls_back_to_rolling(self):
        """An anchor whose window_end is older than the roll-forward
        lookback cap can't be tiled reliably → rolling-5h fallback."""
        with TemporaryDirectory() as tmp:
            now = time.time()
            we = now - 12 * 3600                 # 12h-old window_end → beyond ~10h cap
            ws = we - 5 * 3600
            core = self._core(tmp, [
                usage_entry(now - 2 * 3600, input_=500, request_id="a"),
            ], cal_data={"session_window": {"window_start": ws, "window_end": we,
                                            "anchored_at": ws}})
            total, _, _ = core.scan_window(now=now)
            self.assertEqual(total, 500)         # rolling-5h fallback counts in-window


class ShouldAnchorSessionTests(unittest.TestCase):
    """should_anchor_session / note_anchor_dispatch / clear_anchor_retries:
    fire auto_calibrate to (re-)capture a /usage anchor when the current
    session has none, bounded by cooldown and a consecutive-retry cap."""

    def _core(self, tmp, cal_data=None, **env):
        cf = os.path.join(tmp, "c.json")
        if cal_data is not None:
            with open(cf, "w") as f:
                json.dump(cal_data, f)
        return reload_core({
            "BUDGET_PROJECTS_DIR": tmp,
            "BUDGET_CALIBRATION_FILE": cf,
            "BUDGET_AUTO_CAL_COOLDOWN_SECS": "300",
            **{k: str(v) for k, v in env.items()},
        })

    def test_no_anchor_fires(self):
        with TemporaryDirectory() as tmp:
            core = self._core(tmp)
            self.assertTrue(core.should_anchor_session(now=time.time()))

    def test_current_session_anchored_does_not_fire(self):
        with TemporaryDirectory() as tmp:
            now = time.time()
            core = self._core(tmp, cal_data={"session_window": {
                "window_start": now - 3600, "window_end": now + 3600,
                "anchored_at": now}})
            self.assertFalse(core.should_anchor_session(now=now))

    def test_expired_anchor_fires(self):
        with TemporaryDirectory() as tmp:
            now = time.time()
            core = self._core(tmp, cal_data={"session_window": {
                "window_start": now - 7 * 3600, "window_end": now - 7200,
                "anchored_at": now - 7 * 3600}})
            self.assertTrue(core.should_anchor_session(now=now))

    def test_cooldown_blocks(self):
        with TemporaryDirectory() as tmp:
            now = time.time()
            core = self._core(tmp, cal_data={"auto_cal_state": {
                "last_dispatch_ts": now - 10}})
            self.assertFalse(core.should_anchor_session(now=now))

    def test_retry_cap_blocks(self):
        with TemporaryDirectory() as tmp:
            now = time.time()
            core = self._core(tmp, cal_data={"auto_cal_state": {
                "anchor_retries": 3, "anchor_retry_ts": now - 60}})
            self.assertFalse(core.should_anchor_session(now=now))

    def test_disabled_via_env(self):
        with TemporaryDirectory() as tmp:
            core = self._core(tmp, BUDGET_SESSION_ANCHOR="0")
            self.assertFalse(core.should_anchor_session(now=time.time()))

    def test_note_dispatch_increments_then_clear_resets(self):
        with TemporaryDirectory() as tmp:
            core = self._core(tmp)
            now = time.time()
            core.note_anchor_dispatch(now=now)
            st = core.load_calibration()["auto_cal_state"]
            self.assertEqual(st["anchor_retries"], 1)
            self.assertAlmostEqual(st["last_dispatch_ts"], now, delta=1)
            core.note_anchor_dispatch(now=now + 1)
            self.assertEqual(
                core.load_calibration()["auto_cal_state"]["anchor_retries"], 2)
            core.clear_anchor_retries()
            self.assertEqual(
                core.load_calibration()["auto_cal_state"]["anchor_retries"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
