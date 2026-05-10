#!/usr/bin/env bash
set -e

# Pinned to a release tag (not `main`) so the download target is immutable.
# CI verifies this version stays aligned with the other version-bearing files.
TAG="v1.2.0"
REPO="https://raw.githubusercontent.com/Star001-KR/claude-session-budget/${TAG}"
SUMS_URL="https://github.com/Star001-KR/claude-session-budget/releases/download/${TAG}/SHA256SUMS"
HOOKS_DIR="$HOME/.claude/hooks"

# sha256 helper — Linux ships sha256sum, macOS ships shasum.
if command -v sha256sum >/dev/null 2>&1; then
    sha256_check() { sha256sum -c "$1"; }
elif command -v shasum >/dev/null 2>&1; then
    sha256_check() { shasum -a 256 -c "$1"; }
else
    echo "ERROR: need sha256sum or shasum on PATH" >&2
    exit 1
fi

mkdir -p "$HOOKS_DIR"

# Stage downloads in a temp dir; only move into HOOKS_DIR after sha256 verifies.
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

curl -fsSL "$SUMS_URL" -o "$TMPDIR/SHA256SUMS"
for f in _budget_core.py budget_check.py calibrate.py auto_calibrate.py; do
    curl -fsSL "$REPO/scripts/$f" -o "$TMPDIR/$f"
done

(cd "$TMPDIR" && sha256_check SHA256SUMS)

mv "$TMPDIR/_budget_core.py" "$TMPDIR/budget_check.py" "$TMPDIR/calibrate.py" "$TMPDIR/auto_calibrate.py" "$HOOKS_DIR/"
curl -fsSL "$REPO/.env.example" -o "$HOOKS_DIR/.env.example" || true
chmod +x "$HOOKS_DIR/budget_check.py" "$HOOKS_DIR/calibrate.py" "$HOOKS_DIR/auto_calibrate.py"

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
