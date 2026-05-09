#!/usr/bin/env bash
set -e
REPO="https://raw.githubusercontent.com/Star001-KR/claude-session-budget/main"
HOOKS_DIR="$HOME/.claude/hooks"
mkdir -p "$HOOKS_DIR"
curl -fsSL "$REPO/scripts/_budget_core.py" -o "$HOOKS_DIR/_budget_core.py"
curl -fsSL "$REPO/scripts/budget_check.py" -o "$HOOKS_DIR/budget_check.py"
curl -fsSL "$REPO/scripts/calibrate.py" -o "$HOOKS_DIR/calibrate.py"
curl -fsSL "$REPO/.env.example" -o "$HOOKS_DIR/.env.example" || true
chmod +x "$HOOKS_DIR/budget_check.py" "$HOOKS_DIR/calibrate.py"
python3 - << 'EOF'
import json, os
p = os.path.expanduser("~/.claude/settings.json")
s = json.load(open(p)) if os.path.exists(p) else {}
cmd = "python3 ~/.claude/hooks/budget_check.py"
pre = s.setdefault("hooks", {}).setdefault("PreToolUse", [])
if not any(cmd in str(e) for e in pre):
    pre.append({"matcher": "*", "hooks": [{"type": "command", "command": cmd}]})
    json.dump(s, open(p, "w"), indent=2)
    print("Patched settings.json")
else:
    print("Hook already present")
EOF
echo "Done."
echo "Optional: cp $HOOKS_DIR/.env.example ~/.claude/.env  and edit thresholds."
echo "Optional: python3 $HOOKS_DIR/calibrate.py --observed-pct <NUMBER>  (or just let auto-learning handle it)"
