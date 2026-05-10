# Internals

How `claude-session-budget` actually estimates Claude Code's 5-hour session
usage from local jsonl. This is the deep-dive companion to the README;
the README explains *what to do*, this file explains *why* the moving parts
are shaped the way they are.

## Big picture

The estimator is a **three-layer pipeline** sitting between local jsonl
files and the hook's exit code:

```
~/.claude/projects/**/*.jsonl
        │
        ▼
  ┌────────────────────────────┐
  │ 1. Window scanner          │  cutoff = now − 5h
  │    scan_window()            │  weighted = Σ compute_weighted(usage)
  └────────────────────────────┘
        │
        ▼
  ┌────────────────────────────┐
  │ 2. Signature matcher       │  type=system, subtype=api_error,
  │    _looks_like_rate_limit  │  status=429 / inner type contains
  │                            │  rate_limit / usage_limit
  └────────────────────────────┘
        │
        ▼
  ┌────────────────────────────┐
  │ 3. EWMA calibrator         │  on each new event:
  │    maybe_update_calibration│  limit ← α·observed + (1-α)·prior
  └────────────────────────────┘
        │
        ▼
  pct = weighted / limit  →  exit 0/2 (or sleep+recheck)
```

Each layer is independent and can be tested in isolation. Failure of any one
layer degrades gracefully — EWMA learning frozen falls back to the README
baseline, malformed lines are skipped, etc.

## Layer 1 — Window scan

[`scan_window()`](../scripts/_budget_core.py) walks every jsonl in
`PROJECTS_DIR`, filters by file mtime first (cheap), then per-line
`timestamp >= now − 5h`. There is **no anchor logic** — the cutoff is the
plain rolling-5h boundary.

### Why no `bridge_status` anchor

An earlier version detected `type=system, subtype=bridge_status` entries
and treated the most-recent one as the "session start", raising the cutoff
to that timestamp. The reasoning was that `bridge_status` fires when
`/remote-control` attaches and would mark a fresh 5h window.

Reality check: `bridge_status` fires whenever Claude Code's CLI attaches
to the remote-control bridge, which happens *every* time the user runs
`claude` — many times within one 5h window for active users. Using
`max(bridge_status_ts)` as anchor caused the cutoff to leap forward each
time a new CLI session started, silently zeroing the budget mid-window.

The current implementation ignores `bridge_status` entirely. The trade-off:
we cannot pinpoint the server's exact 5h-window start, so our `oldest`
(earliest in-window usage ts) lags the true anchor by however much idle
time preceded the first jsonl message — usually a few minutes. Activity
on other devices or `claude.ai` web is invisible to local jsonl regardless
of any anchor strategy, so this estimator is structurally a *lower bound*
on the server-side `/usage` number anyway.

### `oldest` semantics

Drives the reset-time estimate (`reset_at = oldest + 5h`):
- ≥1 in-window usage entry → `oldest = earliest in-window usage ts`
- Otherwise → `oldest = now` (effectively "fresh window")

### Token weights — TTL-aware

[`compute_weighted(usage)`](../scripts/_budget_core.py) maps each usage
entry to a cost-equivalent integer using Anthropic's list-price ratios:

| Field | Weight | Notes |
|---|---|---|
| `input_tokens` | 1.00× | base |
| `output_tokens` | 5.00× | |
| `cache_read_input_tokens` | 0.10× | |
| `cache_creation.ephemeral_5m_input_tokens` | 1.25× | default cache TTL |
| `cache_creation.ephemeral_1h_input_tokens` | **2.00×** | extended cache TTL |
| `cache_creation_input_tokens` (legacy) | 1.25× | fallback when no breakdown |

The flat `cache_creation_input_tokens` field equals the TTL-breakdown sum
in current Claude Code builds, so when both are present we consume only
the breakdown — using both would double-count the cache write. Older jsonl
entries that pre-date the breakdown still work via the legacy 1.25×
fallback.

These weights aren't speculation about an unpublished billing formula —
they're the published list prices. Still a proxy (the actual 5h-cap
formula could differ), but matching dollar-cost is the most defensible
prior we have.

## Layer 3 — Signature matcher

[`_looks_like_rate_limit(parsed)`](../scripts/_budget_core.py:148) operates on the
*parsed dict*, not the raw line.

**Match rule:**
1. `parsed["type"] == "system"` AND `parsed["subtype"] == "api_error"` — gate
2. `parsed["error"]["status"] == 429` → match, OR
3. Walk up to 4 levels of nested `error.error.error...`; any `type` field
   containing `rate_limit` or `usage_limit` → match

**Why structural, not regex on the line:**

This is the historically painful one. v1 of this matcher regex-searched the
*full jsonl line* for `5-hour limit`, `rate_limit`, `limit reached`, etc. It
worked until the project itself was being debugged: every "rate limit" the
user typed, every "BLOCKING" we printed back, every conversation about the
tool itself ended up in jsonl as user/assistant message bodies — and the
matcher swallowed all of it as if they were real Anthropic 429s.

EWMA then "learned" the false events' weighted totals as the new limit:

```
prior=43.4M  obs_w=43.2M → 43.3M     ← legit-looking samples
prior=43.4M  obs_w= 1.8M → 30.9M     ← anchor applied, weighted dropped
prior=30.9M  obs_w= 1.9M → 22.2M
prior=22.2M  obs_w= 2.0M → 16.1M     ← ← ← runaway in 4 calls
```

The 16M limit produced false 100% BLOCKING. Reading `~/.claude/.budget_calibration.json`
during that window shows the smooth, monotonic descent — a textbook
self-poisoning loop.

The fix: match the **structural** signature only. Real api_error entries
have a clearly-defined shape:
```json
{
  "type": "system",
  "subtype": "api_error",
  "error": {
    "status": 401,
    "error": {
      "type": "authentication_error",
      "error": {"type": "authentication_error", "message": "..."}
    },
    "headers": {...},
    "requestID": "req_..."
  }
}
```
A real 429 lands in this exact shape with `status: 429` and an inner type
along the lines of `rate_limit_error`. User/assistant message bodies have
`message.role` and `message.content` — they never have `subtype: "api_error"`.

The signature matcher's gate at step 1 cuts the false-positive class to
zero while preserving the legitimate signal.

**Conservatism — empirical status.** The structural shape above (`type=system,
subtype=api_error` with `error.status` as the HTTP code) has been validated
against real captures in production user jsonl (n=17 total):

| `error.status` | Count | Notes |
|---|---|---|
| 401 | 9 | `authentication_error` |
| 502 | 8 | Cloudflare bad gateway, with `retryInMs` / `retryAttempt` metadata |
| 429 | **0** | not captured in any user log to date |

The 401/502 captures confirm our gating shape is correct, but the `status=429`
variant specifically has never appeared in jsonl. The most likely explanation:
Claude Code surfaces rate-limit responses via stdout text ("Claude usage limit
reached.") rather than the jsonl persistence path. Strings-extraction of the
Desktop bundle shows two corroborating signals:

- a stream-message set `new Set(["result", "rate_limit_event"])` — i.e.
  `rate_limit_event` is a *stream event* type, distinct from `api_error`, but
  empirically it does not get persisted to jsonl either
- a Desktop-side text classifier:
  `c.includes("hit your limit") || c.includes("out of extra usage")` →
  `category: "rate_limit"` — i.e. Desktop derives the rate_limit category from
  CLI stdout text, not from a structured jsonl entry

We therefore keep both `status=429` and the inner-type substring match
(`rate_limit` / `usage_limit`) as accept rules: cheap, safe, and the only
sensible cold-start when no real 429 jsonl example exists. Once one is
captured, the inner type can be tightened from "contains `rate_limit`" to
the exact string, and a stdout-pipe text matcher could be added as a parallel
Layer 3 signal if Anthropic does not start persisting 429s to jsonl.

## Layer 4 — EWMA calibrator

[`maybe_update_calibration()`](../scripts/_budget_core.py:295) is invoked on every
hook fire. For each new in-window event (deduped via `seen_events`):

```
new_limit = α · weighted_at_event + (1 − α) · prior_limit
```

Default `α = 0.3`. Lower → more inertia (trust prior). Higher → trust new
observation. Configurable via `BUDGET_EWMA_ALPHA`.

`weighted_at_event` is the running total at the event's timestamp — i.e.
"how many weighted tokens had we used when Anthropic rejected us?" That's
the empirical 100% reading.

Manual calibration ([`record_observed_pct()`](../scripts/_budget_core.py:332)) is
identical math, but the observation comes from `/usage`'s percentage instead
of an api_error event. Useful as a cold start when no rate-limit has fired
yet.

## State files

| Path | Purpose |
|---|---|
| `~/.claude/projects/**/*.jsonl` | Read-only source — Claude Code's own session logs |
| `~/.claude/.budget_calibration.json` | Our state: `limit`, `seen_events`, `history`, `weights` |
| `./.env`, `~/.claude/.env` | User-overridable config |

`seen_events` is keyed by `f"{ts:.0f}"` to make calibration *idempotent* —
running `budget_check.py` 100 times against the same event learns it once.

## Failure modes & their behavior

| Scenario | Behavior |
|---|---|
| jsonl line is malformed | skipped silently |
| jsonl file mtime > 5h old | skipped (mtime optimization) |
| No `bridge_status` in window | fallback to plain 5h rolling |
| No api_error events ever | `limit` stays at `DEFAULT_LIMIT` (63M) |
| `~/.claude/.budget_calibration.json` corrupt | treated as empty, recreated on next save |
| `~/.claude/projects` missing | `weighted = 0`, `pct = 0%` (proceed) |

## Testing

The full suite is in [`tests/test_budget_core.py`](../tests/test_budget_core.py)
(86 tests). Each layer has its own test class:

- `LoadEnvFileTests` — env loader semantics (incl. opt-in cwd `.env`)
- `ParseTsTests` — timestamp parsing edge cases
- `RateLimitSignatureTests` — Layer 3 signature matcher, incl. body-text
  false-positive regression cases
- `ScanWindowTests` — Layer 1 rolling-5h scan, mtime fast-path, malformed
  line tolerance, body-text false-positive guard
- `ScanWindowAnchorTests` — Layer 1 `oldest` semantics under mixed inputs
- `ComputeWeightedTests` — TTL-aware cache_creation accounting (5m vs 1h,
  legacy field supersession)
- `CalibrationTests` / `RecordObservedPctTests` / `MaybeUpdateCalibrationTests`
  — Layer 4 EWMA + atomic save round-trips
- `ThresholdConstantsTests` — env-var binding incl. sleep-mode constants
- `AutoCalibrateTriggerTests` — milestone gating, cooldown, window-key
  rollover for `should_fire_auto_calibrate()`
- `ParseUsageTextTests` — `/usage` panel scraping (ANSI strip, multi-row
  panel, missing-section tolerance)

Tests use `importlib.reload()` to pick up env-var changes since module-level
constants cache them at import time.

## Open questions

- **Does Claude Code ever persist a 429 to jsonl, or only to stdout?**
  Empirically (n=17 api_error captures across one heavy-user's logs) the
  answer so far is "stdout only." If confirmed across more users, Layer 3
  may need a stdout-pipe text matcher (for `"hit your limit"` /
  `"out of extra usage"`) as a parallel signal — but we'd need to be very
  careful to source that text from the CLI subprocess's stderr/stdout, not
  from message-body strings, to avoid re-introducing the self-poisoning
  loop that motivated the structural matcher in the first place.
- **What does Anthropic's real 429 jsonl line look like, if it exists?**
  Captures of 401/502 carry `retryInMs`, `retryAttempt`, `maxRetries`,
  `uuid`, `headers`, `requestID` — we'd expect 429 to follow the same
  shape, but the inner `error.type` exact string and any
  `anthropic-ratelimit-*` headers remain unverified.
- Can `error.headers` from a real 429 carry `anthropic-ratelimit-reset`
  directly? If yes, we can read reset epoch *exactly* instead of estimating.
  (Neither 401 nor 502 captures carried this header, so the data point
  exists only in the unobserved 429 case.)
- Are weights identical across Sonnet / Haiku, or do they need per-model
  multipliers when those models contribute to the same 5h budget?
