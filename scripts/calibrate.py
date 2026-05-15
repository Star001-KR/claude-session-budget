#!/usr/bin/env python3
"""
Manual calibration entry point. Auto-learning from JSONL rate-limit events runs
on every budget_check.py invocation; this script is for periodic /usage readings.

Two input modes:

    # Mode A — explicit percentage (you read the bar yourself).
    python3 calibrate.py --observed-pct 67

    # Mode B — paste the full /usage panel text and let us parse it.
    # Run /usage inside Claude Code, select-copy the panel, then:
    pbpaste | python3 calibrate.py --from-stdin
    # or
    python3 calibrate.py --from-stdin <<EOF
    Current session
    ██████████████████████████████████████████████████ 100% used
    Resets 3:40am (Asia/Seoul)
    ...
    EOF

The result is EWMA-merged into ~/.claude/.budget_calibration.json and picked
up automatically on next run; no env var needed.
"""
import argparse, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _budget_core import (
    record_observed_pct, scan_window, load_calibration,
    CALIBRATION_FILE, EWMA_ALPHA,
)


# ANSI CSI sequences (cursor moves, color, etc.) that show up if the user
# pastes from a TTY-captured stream rather than a clean copy from the panel.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# Match the "Current session" panel's percentage line. The bar is variable
# width / fill char, so we just look for "NN% used" near "Current session".
_PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%\s*used", re.IGNORECASE)
# Reset clue — used to confirm we found the right block, not e.g. weekly.
_RESET_RE = re.compile(r"Resets?\s+([0-9]{1,2}(?::[0-9]{2})?\s*(?:am|pm)?(?:\s*\([^)]*\))?)", re.IGNORECASE)


def parse_usage_text(text):
    """Extract `Current session` percentage from pasted /usage panel text.

    Returns (pct, reset_clue) or (None, None) on no match. Tolerant of:
      - ANSI escape sequences (TTY capture)
      - Unicode block chars in the progress bar (██ ▌ etc.)
      - Extra whitespace / line wrapping

    The /usage panel has multiple percentage rows (Current session, Current
    week (all models), Current week (Sonnet only), Extra usage); we lock onto
    the row right under the literal `Current session` heading.
    """
    if not text:
        return None, None
    text = _ANSI_RE.sub("", text)
    lines = [ln.rstrip() for ln in text.splitlines()]

    # Find the "Current session" heading and look at the next ~6 lines.
    pct = None
    reset = None
    for i, ln in enumerate(lines):
        if "current session" in ln.lower() and "week" not in ln.lower():
            for ln2 in lines[i + 1: i + 7]:
                if pct is None:
                    m = _PCT_RE.search(ln2)
                    if m:
                        pct = float(m.group(1))
                if reset is None:
                    m = _RESET_RE.search(ln2)
                    if m:
                        reset = m.group(0).strip()
                if pct is not None and reset is not None:
                    return pct, reset
            if pct is not None:
                return pct, reset
    return pct, reset


def main():
    p = argparse.ArgumentParser()
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--observed-pct", type=float,
                     help="Current session %% from /usage (1-100)")
    src.add_argument("--from-stdin", action="store_true",
                     help="Parse pasted /usage panel text from stdin")
    args = p.parse_args()

    reset_clue = None
    if args.from_stdin:
        text = sys.stdin.read()
        pct, reset_clue = parse_usage_text(text)
        if pct is None:
            print("ERROR: Could not find 'Current session NN% used' in pasted "
                  "text. Make sure you copied the /usage panel "
                  "(or use --observed-pct).", file=sys.stderr)
            sys.exit(1)
    else:
        pct = args.observed_pct

    if not 1 <= pct <= 100:
        # Allow pct == 100 even when /usage is "extra usage" — the calibration
        # math caps at 100 internally; over-100 readings are clamped.
        if pct > 100:
            print(f"  Note: observed {pct:.1f}% clamped to 100%.", file=sys.stderr)
            pct = 100.0
        else:
            print(f"ERROR: observed pct must be 1–100, got {pct}", file=sys.stderr)
            sys.exit(1)

    weighted, _, _ = scan_window()
    if weighted == 0:
        print("ERROR: No usage entries in last 5h. Make sure Claude Code was active.",
              file=sys.stderr)
        sys.exit(1)

    prior_stored = load_calibration().get("limit")
    new_limit = record_observed_pct(pct, weighted=weighted)

    print(f"\n  Weighted tokens: {weighted:,}")
    print(f"  Observed usage:  {pct:.1f}%")
    if reset_clue:
        print(f"  Reset clue:      {reset_clue}")
    if isinstance(prior_stored, (int, float)) and prior_stored > 0:
        print(f"  Prior limit:     {int(prior_stored):,}")
        print(f"  New limit (EWMA α={EWMA_ALPHA}): {new_limit:,}")
    else:
        print(f"  Prior limit:     none (first calibration — seeding directly)")
        print(f"  New limit:       {new_limit:,}")
    print(f"\nSaved to {CALIBRATION_FILE}")
    print("Auto-loaded on next run; no env var needed.")
    print("\nKnown baselines:")
    print("  Claude Max (5x): ~30,000,000  (measured 2026-05-15, post-dedup)")
    print("  Claude Pro:      unknown — please contribute!")


if __name__ == "__main__":
    main()
