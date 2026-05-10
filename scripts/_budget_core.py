"""
Shared core for session-budget tools. No external deps.

Responsibilities:
  - Load .env files (project ./.env, then ~/.claude/.env). Existing process env wins.
  - Read token-weight and threshold config.
  - Scan ~/.claude/projects/**/*.jsonl in the rolling 5h window.
  - Persist auto-learned calibration to ~/.claude/.budget_calibration.json.
  - Detect rate-limit events in JSONL and EWMA-update the calibrated limit.
"""
import glob, json, os, tempfile, time
from datetime import datetime

WINDOW_SECS = 5 * 3600


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
    except Exception:
        pass


_load_env_file(os.path.join(os.getcwd(), ".env"))
_load_env_file(os.path.expanduser("~/.claude/.env"))


PROJECTS_DIR = os.environ.get("BUDGET_PROJECTS_DIR", os.path.expanduser("~/.claude/projects"))
CALIBRATION_FILE = os.environ.get(
    "BUDGET_CALIBRATION_FILE", os.path.expanduser("~/.claude/.budget_calibration.json")
)

DEFAULT_WEIGHTS = {
    "input_tokens": 1.0,
    "output_tokens": 5.0,
    "cache_creation_input_tokens": 1.25,
    "cache_read_input_tokens": 0.10,
}
DEFAULT_LIMIT = 63_226_913


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


EWMA_ALPHA = _env_float("BUDGET_EWMA_ALPHA", "0.3", minimum=0.0, maximum=1.0)

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
HOOK_MAX_SLEEP_SECS = _env_int("BUDGET_MAX_SLEEP_SECS", "14400", minimum=0)


def load_calibration():
    try:
        with open(CALIBRATION_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_calibration(data):
    """Atomic write: tempfile in the same dir, fsync, then os.replace.

    POSIX rename(2) is atomic within a filesystem, so a reader either sees
    the previous file or the fully written new one — never a truncated or
    half-written intermediate. Protects against SIGINT, ENOSPC, and concurrent
    writers truncating each other mid-dump.
    """
    try:
        os.makedirs(os.path.dirname(CALIBRATION_FILE), exist_ok=True)
        dirpath = os.path.dirname(CALIBRATION_FILE) or "."
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
    except Exception:
        pass


def get_calibrated_limit():
    """Priority: BUDGET_CALIBRATED_LIMIT env > stored calibration > default."""
    env_limit = os.environ.get("BUDGET_CALIBRATED_LIMIT")
    if env_limit:
        try:
            return int(env_limit)
        except ValueError:
            pass
    cal = load_calibration()
    if isinstance(cal.get("limit"), (int, float)) and cal["limit"] > 0:
        return int(cal["limit"])
    return DEFAULT_LIMIT


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
        except Exception:
            return 0.0
    return v / 1000 if v > 1e10 else float(v)


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


def _is_bridge_status(d):
    """Structural check for the bridge_status anchor signal."""
    return (
        isinstance(d, dict)
        and d.get("type") == "system"
        and d.get("subtype") == "bridge_status"
    )


def find_session_anchor(now=None):
    """Latest 'session start' timestamp from jsonl bridge_status entries.

    Claude Code writes a `type=system, subtype=bridge_status` line whenever
    /remote-control becomes active — i.e. when a new session attaches to the
    bridge. That timestamp is a strong (but intermittent) signal of when the
    current 5h window actually began.

    Returns:
        epoch seconds of the most recent in-window bridge_status, or None
        if no signal is found in the last 5h. Callers should fall back to
        their own rolling-window estimate when None.
    """
    if now is None:
        now = time.time()
    cutoff = now - WINDOW_SECS
    latest = None
    for f in glob.glob(f"{PROJECTS_DIR}/**/*.jsonl", recursive=True):
        try:
            if os.path.getmtime(f) < cutoff:
                continue
        except OSError:
            continue
        try:
            with open(f, errors="ignore") as fh:
                for line in fh:
                    if "bridge_status" not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if not _is_bridge_status(d):
                        continue
                    ts = parse_ts(d.get("timestamp"))
                    if cutoff <= ts <= now and (latest is None or ts > latest):
                        latest = ts
        except OSError:
            continue
    return latest


def scan_window(now=None):
    """Scan in-window JSONL entries (single I/O pass).

    Walks each jsonl file once, collecting candidates (ts, weighted,
    rate_limit, is_anchor) in memory; the anchor and effective cutoff are
    decided afterward against that list. Replaces the previous double-pass
    where find_session_anchor() and scan_window() each opened every jsonl
    independently.

    Anchor logic: when a `bridge_status` entry is found within the rolling 5h
    window, its timestamp is treated as the authoritative session start —
    cutoff is raised to that point and only newer messages count. When no
    anchor exists, falls back to the plain 5h rolling window.

    Returns:
        weighted_total (int): sum of weighted usage tokens since cutoff.
        oldest_usage_ts (float): effective session start ts. Equal to the
            anchor when one is found; otherwise the earliest in-window usage
            message ts. Reset time = oldest_usage_ts + WINDOW_SECS.
        rate_limit_events (list[(ts, weighted_at_event)]).
    """
    if now is None:
        now = time.time()
    cutoff_5h = now - WINDOW_SECS
    weights = get_weights()

    raw = []  # (ts, w_inc, rl, is_anchor)
    for f in glob.glob(f"{PROJECTS_DIR}/**/*.jsonl", recursive=True):
        try:
            if os.path.getmtime(f) < cutoff_5h:
                continue
        except OSError:
            continue
        try:
            with open(f, errors="ignore") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    ts = parse_ts(d.get("timestamp"))
                    if ts < cutoff_5h:
                        continue
                    # Anchor must be within (cutoff_5h, now]; future-dated
                    # bridge_status entries are not authoritative.
                    is_anchor = _is_bridge_status(d) and ts <= now
                    u = (d.get("message") or {}).get("usage") or {}
                    w_inc = 0
                    if u:
                        w_inc = int(sum(u.get(k, 0) * w for k, w in weights.items()))
                    rl = _looks_like_rate_limit(d)
                    if w_inc or rl or is_anchor:
                        raw.append((ts, w_inc, rl, is_anchor))
        except OSError:
            continue

    anchor = max((ts for ts, _, _, is_a in raw if is_a), default=None)
    cutoff = anchor if (anchor is not None and anchor > cutoff_5h) else cutoff_5h

    raw.sort(key=lambda e: e[0])

    total = 0
    oldest = anchor if anchor is not None else now
    events = []
    for ts, w_inc, rl, _is_a in raw:
        if ts < cutoff:
            continue
        if w_inc:
            total += w_inc
            if anchor is None and cutoff < ts < oldest:
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
        prior = float(cal.get("limit") or DEFAULT_LIMIT)
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
    if isinstance(cal.get("limit"), (int, float)) and cal["limit"] > 0:
        prior = float(cal["limit"])
        new_limit = int(EWMA_ALPHA * observed_limit + (1 - EWMA_ALPHA) * prior)
    else:
        new_limit = observed_limit
    cal["limit"] = new_limit
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
