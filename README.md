# claude-session-budget

Track Claude Code's 5-hour session usage locally — and automatically pause task queues before hitting the limit.

> **Discovered by reverse-engineering `~/.claude/projects/**/*.jsonl`**  
> No API calls. No web scraping. Pure local file parsing.

## The Problem

Claude Code enforces a **rolling 5-hour session limit**. When running automated task queues or background agents, the session can hit its limit mid-task with no warning.

## How It Works

Claude Code writes every API response to local JSONL files:
~/.claude/projects/<project-path>/<session-id>.jsonl

Each assistant message contains token counts in a `usage` field. By summing these with pricing-ratio weights and calibrating against one `/usage` observation, we estimate session usage in real time.

### Token Weighting (Opus pricing, input = 1.0)

| Token Type | Weight |
|---|---|
| input_tokens | 1.00× |
| cache_creation_input_tokens | 1.25× |
| cache_read_input_tokens | 0.10× |
| output_tokens | 5.00× |

### Calibration

```bash
python3 calibrate.py --observed-pct 67
```

Known baselines:
- **Claude Max (5x):** ~63,226,913 weighted tokens = 100% (measured 2026-05-09)
- **Claude Pro:** unknown — contributions welcome

## Installation

### Option A — Claude Code Hook (Recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/Star001-KR/claude-session-budget/main/install.sh | bash
```

Or manually add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/budget_check.py"}]
      }
    ]
  }
}
```

### Option B — Claude Code Skill

```bash
mkdir -p .claude/skills/session-budget
cp skill/SKILL.md .claude/skills/session-budget/SKILL.md
cp budget_check.py .claude/skills/session-budget/check.py
```

### Option C — PM Layer / Orchestrator

```python
from session_budget_manager import SessionBudgetManager

budget = SessionBudgetManager()

async def dispatch_task(task):
    wait_secs = await budget.check_before_dispatch()
    if wait_secs:
        await asyncio.sleep(wait_secs)
```

## Thresholds

| Threshold | Default | Behavior |
|---|---|---|
| Sync | 80% | Re-reads JSONL and logs updated estimate |
| Pause | 93% | Blocks next dispatch; waits until session resets |

```bash
BUDGET_SYNC_PCT=80 BUDGET_PAUSE_PCT=93 python3 budget_check.py
```

## Limitations

- Token weights are a **proxy** — Anthropic's internal formula is not public
- **Peak hours** (weekday 5–11am PT) consume limits faster
- **Cross-device usage** is not tracked (JSONL files are local only)
- Recalibrate after plan changes

## Files

| File | Description |
|---|---|
| `budget_check.py` | Lightweight hook script (no deps) |
| `session_budget_manager.py` | Full async class for PM/orchestrator integration |
| `calibrate.py` | One-time calibration tool |
| `install.sh` | One-line hook installer |
| `skill/SKILL.md` | Claude Code skill definition |

## Contributing

PRs welcome — especially calibration values for Pro and Max 20x plans.

## License

MIT
