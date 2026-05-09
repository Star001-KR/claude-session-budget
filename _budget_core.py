"""
Shared core for session-budget tools. No external deps.

Responsibilities:
  - Load .env files (project ./.env, then ~/.claude/.env). Existing process env wins.
  - Read token-weight and threshold config.
  - Scan ~/.claude/projects/**/*.jsonl in the rolling 5h window.
  - Persist auto-learned calibration to ~/.claude/.budget_calibration.json.
  - Detect rate-limit events in JSONL and EWMA-update the calibrated limit.
"""
import glob, json, os, re, time
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
EWMA_ALPHA = float(os.environ.get("BUDGET_EWMA_ALPHA", "0.3"))

THRESHOLD_SYNC = float(os.environ.get("BUDGET_SYNC_PCT", "80")) / 100
THRESHOLD_PAUSE = float(os.environ.get("BUDGET_PAUSE_PCT", "93")) / 100


def load_calibration():
    try:
        with open(CALIBRATION_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_calibration(data):
    try:
        os.makedirs(os.path.dirname(CALIBRATION_FILE), exist_ok=True)
        with open(CALIBRATION_FILE, "w") as f:
            json.dump(data, f, indent=2)
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


_RATE_LIMIT_PATTERNS = [
    re.compile(r"5-?hour\s+limit", re.I),
    re.compile(r"session\s+limit", re.I),
    re.compile(r"rate[_\-\s]?limit", re.I),
    re.compile(r"usage\s+limit\s+reached", re.I),
    re.compile(r"limit\s+reached", re.I),
]


def _looks_like_rate_limit(line):
    return any(p.search(line) for p in _RATE_LIMIT_PATTERNS)


def scan_window(now=None):
    """Scan in-window JSONL entries.

    Returns:
        weighted_total (int), oldest_usage_ts (float), rate_limit_events (list[(ts, weighted_at_event)]).
    """
    if now is None:
        now = time.time()
    cutoff = now - WINDOW_SECS
    weights = get_weights()

    entries = []
    for f in glob.glob(f"{PROJECTS_DIR}/**/*.jsonl", recursive=True):
        try:
            if os.path.getmtime(f) < cutoff:
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
                    if ts < cutoff:
                        continue
                    u = (d.get("message") or {}).get("usage") or {}
                    w_inc = 0
                    if u:
                        w_inc = int(sum(u.get(k, 0) * w for k, w in weights.items()))
                    rl = _looks_like_rate_limit(line)
                    if w_inc or rl:
                        entries.append((ts, w_inc, rl))
        except OSError:
            continue

    entries.sort(key=lambda e: e[0])

    total = 0
    oldest = now
    events = []
    for ts, w_inc, rl in entries:
        if w_inc:
            total += w_inc
            if cutoff < ts < oldest:
                oldest = ts
        if rl:
            events.append((ts, total))
    return total, oldest, events


def maybe_update_calibration():
    """Detect new rate-limit events; EWMA-update the stored limit. Returns effective limit."""
    cal = load_calibration()
    seen = set(cal.get("seen_events") or [])
    _, _, events = scan_window()

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
