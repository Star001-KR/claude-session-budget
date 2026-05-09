# Session Budget Awareness

Use when operating in automated or long-running contexts to avoid hitting the 5-hour Claude Code session limit mid-task.

## Check remaining budget

```bash
python3 .claude/skills/session-budget/check.py
```

## Thresholds

| Usage | Action |
|---|---|
| < 80% | Proceed normally |
| 80–93% | Proceed; avoid spawning many subagents |
| ≥ 93% | **Stop.** Report: `BUDGET_PAUSE: session at X% — resets at ~HH:MM. Task suspended.` |

## Note

Reads `~/.claude/projects/**/*.jsonl` locally. No API calls or network requests.
Default limit: 63,226,913 weighted tokens (Claude Max 5x, measured 2026-05-09).
