#!/usr/bin/env bash
set -e

# Pinned to a release tag (not `main`) so the download target is immutable.
# CI verifies this version stays aligned with the other version-bearing files.
TAG="v1.2.1"
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
import json, os, shutil, sys, tempfile, time

p = os.path.expanduser("~/.claude/settings.json")

# Load existing settings, tolerating a corrupt/unreadable file by backing
# it up rather than crashing the installer mid-run.
if os.path.exists(p):
    try:
        with open(p) as f:
            s = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        broken = f"{p}.broken-{int(time.time())}"
        try:
            shutil.copy2(p, broken)
            print(f"WARN: settings.json could not be parsed ({e}); backed up to {broken}", file=sys.stderr)
        except OSError as backup_err:
            print(f"WARN: settings.json could not be parsed ({e}); backup also failed ({backup_err})", file=sys.stderr)
        s = {}
else:
    s = {}

cmd = "python3 ~/.claude/hooks/budget_check.py"
pre = s.setdefault("hooks", {}).setdefault("PreToolUse", [])
if any(cmd in str(e) for e in pre):
    print("Hook already present")
else:
    pre.append({"matcher": "*", "hooks": [{"type": "command", "command": cmd}]})

    # Back up the prior settings.json before overwriting, so a botched
    # install (SIGINT, ENOSPC, etc.) leaves the user a recoverable copy.
    if os.path.exists(p):
        try:
            shutil.copy2(p, p + ".bak")
        except OSError as e:
            print(f"WARN: could not write backup {p}.bak: {e}", file=sys.stderr)

    # Atomic write: tempfile in the same dir, fsync, then os.replace —
    # avoids leaving settings.json as a truncated zero-byte file if the
    # process is interrupted mid-dump.
    dirpath = os.path.dirname(p) or "."
    os.makedirs(dirpath, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".settings_", suffix=".tmp", dir=dirpath)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(s, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    print("Patched settings.json")
EOF
echo "Done."
echo "Optional: cp $HOOKS_DIR/.env.example ~/.claude/.env  and edit thresholds."
echo "Optional: python3 $HOOKS_DIR/calibrate.py --observed-pct <NUMBER>  (or just let auto-learning handle it)"
