"""
Shared core for session-budget tools. No external deps.

Responsibilities:
  - Load .env files (project ./.env, then ~/.claude/.env). Existing process env wins.
  - Read token-weight and threshold config.
  - Scan ~/.claude/projects/**/*.jsonl in the rolling 5h window.
  - Persist auto-learned calibration to ~/.claude/.budget_calibration.json.
  - Detect rate-limit events in JSONL and EWMA-update the calibrated limit.
"""
import glob, json, os, re, tempfile, time, traceback
from datetime import datetime, timedelta

WINDOW_SECS = 5 * 3600

# Where swallowed exceptions get appended so silent hook failures
# (corrupt calibration, unreadable env file, malformed jsonl line, …)
# survive as on-disk tracebacks instead of vanishing.
ERROR_LOG_PATH = os.environ.get(
    "BUDGET_ERROR_LOG", os.path.expanduser("~/.claude/.budget_errors.log")
)
ERROR_LOG_MAX_BYTES = 256 * 1024


def _log_swallowed(exc, context):
    """Append a swallowed exception's traceback to ERROR_LOG_PATH.

    Self-swallows write failures: a failed log write must not become a new
    exception that escapes the hook into the user's tool call.
    """
    try:
        if os.path.exists(ERROR_LOG_PATH) and os.path.getsize(ERROR_LOG_PATH) > ERROR_LOG_MAX_BYTES:
            try:
                os.rename(ERROR_LOG_PATH, ERROR_LOG_PATH + ".old")
            except OSError:
                pass
        os.makedirs(os.path.dirname(ERROR_LOG_PATH) or ".", exist_ok=True)
        with open(ERROR_LOG_PATH, "a") as f:
            ts = datetime.now().isoformat(timespec="seconds")
            f.write(f"[{ts}] {context}: {type(exc).__name__}: {exc}\n")
            f.write(traceback.format_exc())
            f.write("\n")
    except Exception:
        pass


def _load_env_file(path):
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                    v = v[1:-1]
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception as e:
        _log_swallowed(e, f"_load_env_file({path!r})")


# ~/.claude/.env is always loaded (global config under user control).
# ./.env (cwd) is opt-in: importing this module from an untrusted repo
# should not silently inject BUDGET_* (or anything else) into os.environ.
# Set BUDGET_LOAD_PROJECT_ENV=1 in process env or in ~/.claude/.env to
# restore the per-project override behavior.
_load_env_file(os.path.expanduser("~/.claude/.env"))
if os.environ.get("BUDGET_LOAD_PROJECT_ENV", "").strip().lower() in ("1", "true", "yes"):
    _load_env_file(os.path.join(os.getcwd(), ".env"))


PROJECTS_DIR = os.environ.get("BUDGET_PROJECTS_DIR", os.path.expanduser("~/.claude/projects"))
CALIBRATION_FILE = os.environ.get(
    "BUDGET_CALIBRATION_FILE", os.path.expanduser("~/.claude/.budget_calibration.json")
)

# Weights are calibrated against Anthropic's published list-price ratios
# (https://www.anthropic.com/pricing#api). Using cost-equivalent weighting
# matches the dollar-cost intuition behind the 5h Max session cap better
# than naive token counting.
#
# Cache writes ship in two TTL flavors that price differently:
#   - 5min default TTL  -> 1.25× base input price
#   - 1h extended TTL   -> 2.00× base input price
# The legacy `cache_creation_input_tokens` total has no TTL breakdown so we
# keep it at 1.25× (the historical default and a safe lower bound for files
# written before Claude Code added the per-TTL fields).
DEFAULT_WEIGHTS = {
    "input_tokens": 1.0,
    "output_tokens": 5.0,
    "cache_creation_input_tokens": 1.25,        # legacy fallback (no TTL split)
    "cache_creation_5m_input_tokens": 1.25,     # ephemeral_5m_input_tokens
    "cache_creation_1h_input_tokens": 2.0,      # ephemeral_1h_input_tokens
    "cache_read_input_tokens": 0.10,
}
# Post-dedup Claude Max (5x) baseline: weighted tokens for one full 5h
# window at 100%. Re-derived 2026-05-15 from the Slack-incident window
# after the scan_window content-block dedup fix — the pre-dedup figure
# (~63.2M) was inflated ~3.3x by counting one API response once per
# content-block jsonl line. Seed only; calibration refines it per /usage.
DEFAULT_LIMIT = 30_000_000


def _env_float(name, default, minimum=None, maximum=None):
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)
    if minimum is not None and value < minimum:
        return float(default)
    if maximum is not None and value > maximum:
        return float(default)
    return value


def _env_int(name, default, minimum=None):
    try:
        value = int(_env_float(name, default))
    except (TypeError, ValueError):
        value = int(default)
    if minimum is not None and value < minimum:
        return int(default)
    return value


EWMA_ALPHA = _env_float("BUDGET_EWMA_ALPHA", "0.35", minimum=0.0, maximum=1.0)

THRESHOLD_SYNC = _env_float("BUDGET_SYNC_PCT", "80", minimum=1, maximum=100) / 100
THRESHOLD_PAUSE = _env_float("BUDGET_PAUSE_PCT", "93", minimum=1, maximum=100) / 100
if THRESHOLD_SYNC > THRESHOLD_PAUSE:
    THRESHOLD_SYNC = 0.80
    THRESHOLD_PAUSE = 0.93

HOOK_PAUSE_MODE = os.environ.get("BUDGET_PAUSE_MODE", "block").strip().lower()
if HOOK_PAUSE_MODE not in ("block", "sleep"):
    HOOK_PAUSE_MODE = "block"

HOOK_RECHECK_SECS = _env_int("BUDGET_RECHECK_SECS", "60", minimum=1)
HOOK_RESET_GRACE_SECS = _env_int("BUDGET_RESET_GRACE_SECS", "60", minimum=0)

# Auto-calibration milestones — pct thresholds at which the hook fires
# auto_calibrate.py in the background to refine the limit estimate against
# real `/usage` output. Each milestone fires AT MOST ONCE per 5h window.
# Default: 90% only — a single calibration at the sweet spot where enough
# weighted tokens have accumulated for an accurate reading without being
# too close to the limit. Override with e.g. BUDGET_AUTO_CAL_MILESTONES="80,93"
# to fire multiple times per window.
def _parse_milestones(raw):
    out = []
    for chunk in (raw or "").replace(";", ",").split(","):
        chunk = chunk.strip().rstrip("%")
        if not chunk:
            continue
        try:
            v = float(chunk)
        except ValueError:
            continue
        if 1 <= v <= 100:
            out.append(v / 100)
    return sorted(set(out)) or [0.90]

AUTO_CAL_MILESTONES = _parse_milestones(
    os.environ.get("BUDGET_AUTO_CAL_MILESTONES", "90")
)
AUTO_CAL_COOLDOWN_SECS = _env_int("BUDGET_AUTO_CAL_COOLDOWN_SECS", "300", minimum=0)
AUTO_CAL_ENABLED = os.environ.get(
    "BUDGET_AUTO_CAL_ENABLED", "1"
).strip().lower() in ("1", "true", "yes")
HOOK_MAX_SLEEP_SECS = _env_int("BUDGET_MAX_SLEEP_SECS", "14400", minimum=0)

# Session-window anchoring. When enabled, scan_window aligns its cutoff to
# the real 5h session boundary — captured from /usage's "Resets" clue (see
# parse_reset_to_ts / get_session_anchor) — instead of a plain now-5h
# rolling window. A rolling window straddles real resets: right after a
# reset it keeps summing the previous, already-expired session's tail. Set
# BUDGET_SESSION_ANCHOR=0 to disable and fall back to rolling-5h.
SESSION_ANCHOR_ENABLED = os.environ.get(
    "BUDGET_SESSION_ANCHOR", "1"
).strip().lower() in ("1", "true", "yes")


def load_calibration():
    try:
        with open(CALIBRATION_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        _log_swallowed(e, "load_calibration")
        return {}


def save_calibration(data):
    """Atomic write: tempfile in the same dir, fsync, then os.replace.

    POSIX rename(2) is atomic within a filesystem, so a reader either sees
    the previous file or the fully written new one — never a truncated or
    half-written intermediate. Protects against SIGINT, ENOSPC, and concurrent
    writers truncating each other mid-dump.
    """
    try:
        dirpath = os.path.dirname(CALIBRATION_FILE) or "."
        os.makedirs(dirpath, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".budget_cal_", suffix=".tmp", dir=dirpath)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, CALIBRATION_FILE)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        _log_swallowed(e, "save_calibration")


def _stored_limit(cal):
    """Return the calibrated limit from `cal`, but only if it is post-dedup.

    Calibration files written before the scan_window content-block dedup
    fix hold a `limit` derived from ~3.3x-inflated weighted totals. They
    lack the `counts_deduped` marker; we ignore their `limit` so the
    install base auto-migrates to DEFAULT_LIMIT on upgrade instead of
    silently flipping to under-prediction. Returns None when there is no
    trustworthy stored limit.
    """
    if (cal.get("counts_deduped")
            and isinstance(cal.get("limit"), (int, float))
            and cal["limit"] > 0):
        return int(cal["limit"])
    return None


def get_calibrated_limit():
    """Priority: BUDGET_CALIBRATED_LIMIT env > stored calibration > default."""
    env_limit = os.environ.get("BUDGET_CALIBRATED_LIMIT")
    if env_limit:
        try:
            return int(env_limit)
        except ValueError:
            pass
    stored = _stored_limit(load_calibration())
    return stored if stored is not None else DEFAULT_LIMIT


def auto_calibrate_supported():
    """Whether auto-calibration's pty spawn can run on this platform.

    POSIX (macOS, Linux): True if stdlib `pty` imports — always, in
    practice. Windows: True iff the optional `pywinpty` package is
    installed (`pip install pywinpty`).

    The base jsonl-scan path (everything else in this module) is
    cross-platform; only auto-calibration's `claude /usage` capture
    needs a real pty backend.
    """
    import sys as _sys
    if _sys.platform == "win32":
        try:
            import winpty  # noqa: F401
            return True
        except ImportError:
            return False
    try:
        import pty  # noqa: F401
        return True
    except ImportError:
        return False


def should_fire_auto_calibrate(pct, oldest_ts, cal=None, now=None):
    """Decide whether the hook should kick off auto_calibrate.py.

    Fires once per AUTO_CAL_MILESTONES band per 5h session window. Window
    identity comes from `oldest_ts` (rounded to 30 min) — when the user
    starts a fresh window, oldest_ts moves forward enough that the bucket
    changes and milestones reset.

    Cooldown semantics: `last_dispatch_ts` tracks when the hook last
    spawned a child. Distinct from `last_success_ts` (set by the child
    after a successful calibration) so the hook never sees its own
    dispatch as a cooldown trigger that blocks the same dispatch.

    Platform: no-op on Windows (pty unavailable). See
    `auto_calibrate_supported()`.

    Returns the milestone pct (e.g. 0.80) to fire, or None.
    """
    if not AUTO_CAL_ENABLED:
        return None
    if not auto_calibrate_supported():
        return None  # Windows / pty-less environment
    if os.environ.get("BUDGET_AUTO_CALIBRATE_RUNNING") == "1":
        return None  # never fire from inside an auto-cal child
    if cal is None:
        cal = load_calibration()
    if now is None:
        now = time.time()

    state = cal.get("auto_cal_state") or {}
    last_dispatch = state.get("last_dispatch_ts") or 0
    if now - last_dispatch < AUTO_CAL_COOLDOWN_SECS:
        return None  # a child was recently dispatched — let it complete first

    # Window key: 30-min bucket of `oldest_ts`. New 5h session shifts oldest
    # by hours, so the bucket changes; small clock drift within one session
    # keeps the same bucket.
    window_key = int(oldest_ts // 1800) if oldest_ts else 0
    fired = list(state.get("fired") or [])
    if state.get("window_key") != window_key:
        fired = []  # new window → milestones reset

    for m in AUTO_CAL_MILESTONES:
        if pct >= m and m not in fired:
            return m
    return None


def mark_milestone_fired(milestone, oldest_ts, cal=None, now=None):
    """Hook-side: record dispatch of `milestone` this window. Persists."""
    if cal is None:
        cal = load_calibration()
    if now is None:
        now = time.time()
    window_key = int(oldest_ts // 1800) if oldest_ts else 0
    state = cal.get("auto_cal_state") or {}
    if state.get("window_key") != window_key:
        state = {"window_key": window_key, "fired": []}
    fired = list(state.get("fired") or [])
    if milestone not in fired:
        fired.append(milestone)
    state["fired"] = sorted(fired)
    state["last_dispatch_ts"] = now      # hook-side: when we spawned a child
    state["window_key"] = window_key
    cal["auto_cal_state"] = state
    save_calibration(cal)
    return state


_ANCHOR_MAX_RETRIES = 3  # consecutive /usage anchor attempts before backing off


def should_anchor_session(cal=None, now=None):
    """Whether auto_calibrate should run to (re-)capture a /usage session
    anchor. True when the current session has no valid anchor — none on
    file, or the stored one has expired (now past window_end). Gated by
    the auto-cal cooldown and a small consecutive-retry cap so a broken
    /usage capture can't spawn `claude` without bound.
    """
    if not (SESSION_ANCHOR_ENABLED and AUTO_CAL_ENABLED):
        return False
    if not auto_calibrate_supported():
        return False
    if os.environ.get("BUDGET_AUTO_CALIBRATE_RUNNING") == "1":
        return False
    if cal is None:
        cal = load_calibration()
    if now is None:
        now = time.time()
    anchor = get_session_anchor(cal)
    if anchor is not None and anchor[0] <= now <= anchor[1]:
        return False  # current session already anchored
    state = cal.get("auto_cal_state") or {}
    if now - (state.get("last_dispatch_ts") or 0) < AUTO_CAL_COOLDOWN_SECS:
        return False  # a child was just dispatched — let it finish
    retries = state.get("anchor_retries") or 0
    if (retries >= _ANCHOR_MAX_RETRIES
            and now - (state.get("anchor_retry_ts") or 0) < WINDOW_SECS):
        return False  # capped — back off ~one session length before re-probing
    return True


def note_anchor_dispatch(cal=None, now=None):
    """Hook-side: record that auto_calibrate was spawned for a session
    anchor. Sets the cooldown anchor (last_dispatch_ts) and bumps the
    consecutive-retry counter; a long gap since the last try resets it
    (a fresh session deserves a fresh budget of retries)."""
    if cal is None:
        cal = load_calibration()
    if now is None:
        now = time.time()
    state = cal.get("auto_cal_state") or {}
    retries = state.get("anchor_retries") or 0
    if now - (state.get("anchor_retry_ts") or 0) >= WINDOW_SECS:
        retries = 0
    state["anchor_retries"] = retries + 1
    state["anchor_retry_ts"] = now
    state["last_dispatch_ts"] = now
    cal["auto_cal_state"] = state
    save_calibration(cal)


def clear_anchor_retries(cal=None):
    """Reset the anchor retry counter — called once a session is
    successfully anchored so the next session starts with a full budget."""
    if cal is None:
        cal = load_calibration()
    state = cal.get("auto_cal_state") or {}
    if state.get("anchor_retries"):
        state["anchor_retries"] = 0
        cal["auto_cal_state"] = state
        save_calibration(cal)


def get_weights():
    w = dict(DEFAULT_WEIGHTS)
    cal = load_calibration()
    for k, v in (cal.get("weights") or {}).items():
        if k in w:
            try:
                w[k] = float(v)
            except (TypeError, ValueError):
                pass
    return w


def parse_ts(v):
    if not v:
        return 0.0
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except Exception as e:
            _log_swallowed(e, f"parse_ts({v!r})")
            return 0.0
    return v / 1000 if v > 1e10 else float(v)


# /usage "Resets" clue → timestamp. Matches "12am", "3:40am", "11:30pm".
_RESET_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.IGNORECASE)


def parse_reset_to_ts(reset_clue, now=None):
    """Convert a /usage 'Resets' clue (e.g. 'Resets 12am (Asia/Seoul)')
    into the Unix timestamp of the next occurrence of that wall-clock time.

    Resolved in the machine's LOCAL time. Claude Code's /usage panel shows
    the reset in the account timezone; the budget tool runs on the same
    machine, so local time == panel time in the normal case. If the
    machine tz differs from the panel tz the result is off by the offset
    — an accepted edge case for this dependency-free module; a wildly
    wrong result is caught by session_window_is_valid()'s range check.

    Returns a float timestamp, or None when the clue can't be parsed.
    """
    if not reset_clue:
        return None
    m = _RESET_TIME_RE.search(reset_clue)
    if not m:
        return None
    try:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
    except (TypeError, ValueError):
        return None
    if not (1 <= hour <= 12) or not (0 <= minute <= 59):
        return None
    if m.group(3).lower() == "am":
        hour = 0 if hour == 12 else hour
    else:
        hour = 12 if hour == 12 else hour + 12
    if now is None:
        now = time.time()
    try:
        target = datetime.fromtimestamp(now).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        ts = target.timestamp()
        if ts <= now:
            ts = (target + timedelta(days=1)).timestamp()
        return ts
    except Exception as e:
        _log_swallowed(e, f"parse_reset_to_ts({reset_clue!r})")
        return None


def _looks_like_rate_limit(parsed):
    """Detect a real Anthropic API rate-limit error in a parsed jsonl entry.

    Real signal — Claude Code records API errors as
    `type=system, subtype=api_error` with the full HTTP response captured
    under `error`. We match on:
      - HTTP status 429, OR
      - any nested `error.type` containing "rate_limit" / "usage_limit".

    We deliberately do NOT match on free-text content. User and assistant
    message bodies routinely discuss "rate limit" / "limit reached" as a
    topic (e.g. debugging this very tool) and that produced a self-poisoning
    EWMA learning loop. Structural signature matching eliminates that class
    of false positive entirely.

    Empirical status (n=17 api_error captures from one heavy-user's logs):
    this matcher returned 0 hits — 9×status=401, 8×status=502, 0×status=429.
    Claude Code likely surfaces rate-limit via stdout text ("Claude usage
    limit reached.") rather than persisting to jsonl. The matcher is kept
    as forward-compatible insurance for the day a real 429 shows up in
    jsonl; until then, live calibration runs through record_observed_pct()
    (manual /usage paste) and auto_calibrate.py (background pty spawn).
    See docs/internals.md Layer 3 for the refinement plan once captured.
    """
    if not isinstance(parsed, dict):
        return False
    if parsed.get("type") != "system" or parsed.get("subtype") != "api_error":
        return False
    err = parsed.get("error") or {}
    if not isinstance(err, dict):
        return False
    if err.get("status") == 429:
        return True
    # Walk possibly-nested {"error": {"error": {...}}} shapes (Anthropic SDK)
    cur = err
    for _ in range(4):
        if not isinstance(cur, dict):
            break
        et = cur.get("type")
        if isinstance(et, str) and ("rate_limit" in et or "usage_limit" in et):
            return True
        cur = cur.get("error")
    return False


def compute_weighted(usage, weights=None):
    """Compute weighted token cost for a single usage entry.

    Prefers the per-TTL breakdown under `usage.cache_creation` over the
    legacy `cache_creation_input_tokens` field — when both are present
    (which is the normal case in current Claude Code builds), the legacy
    flat sum equals the breakdown sum, so taking both would double-count.

    Args:
        usage: the `message.usage` dict from a jsonl entry, or None.
        weights: optional weights override; falls back to get_weights().
    """
    if not usage:
        return 0
    if weights is None:
        weights = get_weights()
    total = 0.0
    total += usage.get("input_tokens", 0) * weights.get("input_tokens", 1.0)
    total += usage.get("output_tokens", 0) * weights.get("output_tokens", 5.0)
    total += usage.get("cache_read_input_tokens", 0) * weights.get("cache_read_input_tokens", 0.10)

    cc = usage.get("cache_creation")
    has_breakdown = isinstance(cc, dict) and (
        "ephemeral_5m_input_tokens" in cc or "ephemeral_1h_input_tokens" in cc
    )
    if has_breakdown:
        total += cc.get("ephemeral_5m_input_tokens", 0) * weights.get(
            "cache_creation_5m_input_tokens", 1.25
        )
        total += cc.get("ephemeral_1h_input_tokens", 0) * weights.get(
            "cache_creation_1h_input_tokens", 2.0
        )
    else:
        # Pre-breakdown jsonl entries: fall back to the flat field.
        total += usage.get("cache_creation_input_tokens", 0) * weights.get(
            "cache_creation_input_tokens", 1.25
        )
    return int(total)


_ANCHOR_SKEW_SECS = 120  # clock-skew tolerance for the window-end range check


def get_session_anchor(cal=None):
    """Return (window_start, window_end) from the stored /usage anchor,
    or None when no usable anchor is on file."""
    if cal is None:
        cal = load_calibration()
    sw = cal.get("session_window")
    if not isinstance(sw, dict):
        return None
    ws, we = sw.get("window_start"), sw.get("window_end")
    if isinstance(ws, (int, float)) and isinstance(we, (int, float)) and 0 < ws < we:
        return float(ws), float(we)
    return None


def session_window_is_valid(window_end, now=None):
    """Whether a freshly-read /usage reset time is trustworthy as the
    *current* session's end. Valid only in (now, now+5h] (± clock skew):
    a lagging /usage still showing the just-expired session reports a
    window_end <= now, which is rejected so the caller re-reads later."""
    if not isinstance(window_end, (int, float)) or window_end <= 0:
        return False
    if now is None:
        now = time.time()
    return (now - _ANCHOR_SKEW_SECS) < window_end <= (now + WINDOW_SECS + _ANCHOR_SKEW_SECS)


def save_session_anchor(window_end, now=None):
    """Persist a /usage-derived session window (window_start = window_end
    - 5h). No-op returning False when window_end fails the validity check."""
    if now is None:
        now = time.time()
    if not session_window_is_valid(window_end, now):
        return False
    cal = load_calibration()
    cal["session_window"] = {
        "window_end": float(window_end),
        "window_start": float(window_end) - WINDOW_SECS,
        "anchored_at": now,
    }
    save_calibration(cal)
    return True


def _session_cutoff(now, usage_timestamps):
    """Return (cutoff, is_anchored) for the 5h session containing `now`.

    cutoff is the session START — scan_window counts from here, not from
    now-5h. is_anchored is True when the cutoff came from a /usage anchor
    (so reset = cutoff + 5h is trustworthy). `usage_timestamps` must be
    ascending; it drives roll-forward across session boundaries.

    5h sessions do not tile on a fixed grid: a session ends 5h after it
    starts, and the *next* one begins on the first request after that —
    so we roll forward by activity gaps, never by adding 5h blindly.
    """
    rolling = now - WINDOW_SECS
    if not SESSION_ANCHOR_ENABLED:
        return rolling, False
    anchor = get_session_anchor()
    if anchor is None:
        return rolling, False
    ws, we = anchor
    if ws <= now <= we:
        return ws, True                  # inside the anchored session
    if now < ws:
        return rolling, False            # anchor in the future → clock skew, bail
    # Anchored session expired; roll forward to the session holding `now`.
    seg_end = we
    for _ in range(8):                   # bounded catch-up (8 x 5h)
        nxt = next((t for t in usage_timestamps if t > seg_end), None)
        if nxt is None:
            return now, True             # gap — no active session, 0 usage
        if now <= nxt + WINDOW_SECS:
            return nxt, True             # session containing `now`
        seg_end = nxt + WINDOW_SECS
    return rolling, False                # gave up rolling forward → fallback


def scan_window(now=None):
    """Scan in-window JSONL entries and total weighted usage for the
    current 5h session (single I/O pass).

    The cutoff is anchored to the real session boundary when a /usage
    "Resets" anchor is on file (see parse_reset_to_ts / get_session_anchor):
    scan_window counts from the session START, not from a plain now-5h
    rolling window. A rolling window straddles real session resets — right
    after a reset it keeps summing the previous, already-expired session's
    tail. With no anchor (or BUDGET_SESSION_ANCHOR=0) it falls back to
    rolling-5h. `bridge_status` entries are never used for anchoring (they
    fire on every /remote-control attach, not on a 5h reset).

    Returns:
        weighted_total (int): weighted usage since the session cutoff,
            de-duplicated per requestId — Claude Code logs one jsonl line
            per content block, each repeating the same message.usage.
        session_start (float): start of the current session. Reset time =
            session_start + WINDOW_SECS (exact when anchored); falls back
            to earliest in-window usage when unanchored.
        rate_limit_events (list[(ts, weighted_at_event)]).
    """
    if now is None:
        now = time.time()
    rolling_cutoff = now - WINDOW_SECS
    weights = get_weights()

    raw = []  # (ts, w_inc, rl, msg_key)
    for f in glob.glob(f"{PROJECTS_DIR}/**/*.jsonl", recursive=True):
        try:
            if os.path.getmtime(f) < rolling_cutoff:
                continue
        except OSError:
            continue
        try:
            with open(f, errors="ignore") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = parse_ts(d.get("timestamp"))
                    if ts < rolling_cutoff:
                        continue
                    msg = d.get("message") or {}
                    u = msg.get("usage") or {}
                    w_inc = compute_weighted(u, weights) if u else 0
                    rl = _looks_like_rate_limit(d)
                    if w_inc or rl:
                        raw.append((ts, w_inc, rl, d.get("requestId") or msg.get("id")))
        except OSError:
            continue

    raw.sort(key=lambda e: e[0])

    # Anchor the cutoff to the real session start instead of now-5h.
    usage_ts = [ts for ts, w_inc, _, _ in raw if w_inc]
    cutoff, anchored = _session_cutoff(now, usage_ts)

    # One API response carries one requestId, but Claude Code writes a
    # separate jsonl line per content block (thinking, text, each
    # tool_use), repeating the identical message.usage on each. Charge
    # each request once, keyed on requestId (message.id fallback);
    # keyless entries can't be deduped and are each counted.
    total = 0
    oldest = cutoff if anchored else now
    events = []
    counted = set()
    for ts, w_inc, rl, msg_key in raw:
        if ts < cutoff:
            continue
        if w_inc and (msg_key is None or msg_key not in counted):
            if msg_key is not None:
                counted.add(msg_key)
            total += w_inc
            if not anchored and ts < oldest:
                oldest = ts
        if rl:
            events.append((ts, total))
    return total, oldest, events


def maybe_update_calibration(scan_result=None):
    """Detect new rate-limit events; EWMA-update the stored limit. Returns effective limit.

    Pass `scan_result` (the tuple returned by scan_window()) to reuse a scan
    the caller already performed — avoids re-walking JSONL twice per hook.
    """
    cal = load_calibration()
    seen = set(cal.get("seen_events") or [])
    if scan_result is None:
        scan_result = scan_window()
    _, _, events = scan_result

    changed = False
    for ts, weighted_at_event in events:
        key = f"{ts:.0f}"
        if key in seen or weighted_at_event <= 0:
            continue
        seen.add(key)
        prior = float(_stored_limit(cal) or DEFAULT_LIMIT)
        new_limit = int(EWMA_ALPHA * weighted_at_event + (1 - EWMA_ALPHA) * prior)
        cal["limit"] = new_limit
        cal.setdefault("history", []).append({
            "ts": ts,
            "kind": "rate_limit_detected",
            "observed_weighted": weighted_at_event,
            "prior_limit": int(prior),
            "limit_after_ewma": new_limit,
        })
        changed = True

    if changed:
        cal["counts_deduped"] = True
        cal["seen_events"] = sorted(seen)
        save_calibration(cal)

    return get_calibrated_limit()


def record_observed_pct(observed_pct, weighted=None):
    """Manual calibration: take a /usage % reading and EWMA-update stored limit."""
    if weighted is None:
        weighted, _, _ = scan_window()
    if weighted <= 0 or observed_pct <= 0:
        return None
    observed_limit = int(weighted / (observed_pct / 100))
    cal = load_calibration()
    prior = _stored_limit(cal)
    if prior:
        new_limit = int(EWMA_ALPHA * observed_limit + (1 - EWMA_ALPHA) * prior)
    else:
        new_limit = observed_limit
    cal["limit"] = new_limit
    cal["counts_deduped"] = True
    cal.setdefault("history", []).append({
        "ts": time.time(),
        "kind": "manual",
        "observed_pct": observed_pct,
        "observed_weighted": weighted,
        "observed_limit": observed_limit,
        "limit_after_ewma": new_limit,
    })
    save_calibration(cal)
    return new_limit
