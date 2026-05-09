# Internals

How `claude-session-budget` actually estimates Claude Code's 5-hour session
usage from local jsonl. This is the deep-dive companion to the README;
the README explains *what to do*, this file explains *why* the moving parts
are shaped the way they are.

## Big picture

The estimator is a **four-layer pipeline** sitting between local jsonl files
and the hook's exit code:

```
~/.claude/projects/**/*.jsonl
        │
        ▼
  ┌────────────────────────────┐
  │ 1. Anchor detector         │  bridge_status ts → session start
  │    find_session_anchor()   │
  └────────────────────────────┘
        │
        ▼
  ┌────────────────────────────┐
  │ 2. Window scanner          │  cutoff = max(now-5h, anchor)
  │    scan_window()           │  weighted = Σ(usage × pricing weights)
  └────────────────────────────┘
        │
        ▼
  ┌────────────────────────────┐
  │ 3. Signature matcher       │  type=system, subtype=api_error,
  │    _looks_like_rate_limit  │  status=429 / inner type contains
  │                            │  rate_limit / usage_limit
  └────────────────────────────┘
        │
        ▼
  ┌────────────────────────────┐
  │ 4. EWMA calibrator         │  on each new event:
  │    maybe_update_calibration│  limit ← α·observed + (1-α)·prior
  └────────────────────────────┘
        │
        ▼
  pct = weighted / limit  →  exit 0/2 (or sleep+recheck)
```

Each layer is independent and can be tested in isolation. Failure of any one
layer degrades gracefully — anchor missing falls back to plain rolling
window, EWMA learning frozen falls back to the README baseline, etc.

## Layer 1 — Anchor detection

**Problem:** A pure 5-hour rolling window over-counts. If the user's day
straddles two distinct Anthropic sessions (one ending around hour-5, a fresh
one starting), our window naively sums both — producing 100%+ readings while
the real `/usage` shows 1–2%.

**Signal:** Claude Code writes a `type=system, subtype=bridge_status` line
whenever `/remote-control` activates. That timestamp is a strong indicator of
"a new session is now attached."

**Behavior:** [`find_session_anchor()`](_budget_core.py:153) returns the most
recent in-window `bridge_status` ts, or `None` when no signal is present.

**Caveats:**
- The signal is *intermittent*. Short tool restarts may not produce one.
- If the user idles longer than 5h, the most recent `bridge_status` rolls out
  of the window and we revert to plain rolling — correct behavior.
- We do **not** apply an "anchor reset must be earlier than our estimate"
  precedence rule. The anchor *is* the more authoritative source; second-
  guessing it just reintroduces the bug we set out to fix.

## Layer 2 — Window scan

[`scan_window()`](_budget_core.py:195) walks every jsonl in `PROJECTS_DIR`,
filters by file mtime first (cheap), then per-line `timestamp >= cutoff`.

**Cutoff calculation:**
```python
cutoff = now - WINDOW_SECS
anchor = find_session_anchor(now)
if anchor is not None and anchor > cutoff:
    cutoff = anchor   # raise the floor
```

**`oldest` semantics** (drives reset estimate):
- Anchor present → `oldest = anchor` (regardless of msg ts)
- Anchor absent → `oldest = earliest in-window usage msg ts`

So `reset_at = oldest + 5h` always reflects the active session's start,
whether learned from the explicit signal or inferred from the oldest message.

**Token weights** are pricing-ratio multipliers (input=1.0× as base):
| Field | Weight |
|---|---|
| `input_tokens` | 1.00× |
| `cache_creation_input_tokens` | 1.25× |
| `cache_read_input_tokens` | 0.10× |
| `output_tokens` | 5.00× |

These derive from Anthropic's published Opus pricing. They're a *proxy* — the
internal billing formula isn't public — but the relative ratios hold across
model tiers.

## Layer 3 — Signature matcher

[`_looks_like_rate_limit(parsed)`](_budget_core.py:140) operates on the
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

**Conservatism:** We accept both `status=429` and inner-type substring
matches because we haven't directly observed a real 429 jsonl line in the
wild. Once one shows up, the inner type can be tightened from "contains
`rate_limit`" to the exact string.

## Layer 4 — EWMA calibrator

[`maybe_update_calibration()`](_budget_core.py:264) is invoked on every
hook fire. For each new in-window event (deduped via `seen_events`):

```
new_limit = α · weighted_at_event + (1 − α) · prior_limit
```

Default `α = 0.3`. Lower → more inertia (trust prior). Higher → trust new
observation. Configurable via `BUDGET_EWMA_ALPHA`.

`weighted_at_event` is the running total at the event's timestamp — i.e.
"how many weighted tokens had we used when Anthropic rejected us?" That's
the empirical 100% reading.

Manual calibration ([`record_observed_pct()`](_budget_core.py:295)) is
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
(44 tests). Each layer has its own test class:

- `LoadEnvFileTests` — env loader semantics
- `ParseTsTests` — timestamp parsing edge cases
- `RateLimitSignatureTests` — Layer 3 (10 cases incl. negative/regression)
- `ScanWindowTests` — Layer 2 fundamentals + body-text false-positive guard
- `FindSessionAnchorTests` — Layer 1
- `ScanWindowAnchorTests` — Layer 1+2 integration
- `CalibrationTests` / `RecordObservedPctTests` / `MaybeUpdateCalibrationTests`
  — Layer 4
- `ThresholdConstantsTests` — env-var binding incl. sleep-mode constants

Tests use `importlib.reload()` to pick up env-var changes since module-level
constants cache them at import time.

## Open questions

- What does Anthropic's real 429 jsonl line look like? (Tightens Layer 3.)
- Can `error.headers` from a real 429 carry `anthropic-ratelimit-reset`
  directly? If yes, we can read reset epoch *exactly* instead of estimating.
- Are weights identical across Sonnet / Haiku, or do they need per-model
  multipliers when those models contribute to the same 5h budget?
