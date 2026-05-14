#!/usr/bin/env python3
"""
auto_calibrate.py — fire-and-forget background worker that:

1. Spawns `claude` in a pseudo-TTY (so slash commands work).
2. Sends `/usage` once the input prompt is ready.
3. Captures the panel output, ESC-dismisses, and quits claude.
4. Strips ANSI, parses with calibrate.parse_usage_text(), and EWMA-merges
   the observed % into ~/.claude/.budget_calibration.json.

Invoked by budget_check.py when crossing AUTO_CAL_MILESTONES (default
90%) for the first time in the current 5h window. Designed so the user
never has to copy-paste anything.

Recursion guard: sets BUDGET_AUTO_CALIBRATE_RUNNING=1 in the spawned
claude env. budget_check.py honors that and short-circuits, so no
runaway nesting even if the spawned claude triggers tool calls.

Token cost: each invocation is one fresh `claude` startup (system prompt
+ CLAUDE.md + MCP handshakes get cache-written). Bounded by milestone
firing count — at most 1 invocation per 5h window by default.

Cross-platform PTY:
- POSIX (macOS, Linux):  stdlib `pty` + `subprocess.Popen` + `select`
- Windows:               third-party `pywinpty` + reader thread + queue
On Windows, install the optional dep:  pip install pywinpty
Without it, the worker logs "platform not supported" and exits cleanly,
and the hook never even spawns it (gated by `auto_calibrate_supported()`).

Logs to ~/.claude/.budget_auto_calibrate.log (truncate-on-rotate, ~256KB).

Exit codes:
    0  calibrated successfully (or cooldown / disabled / unsupported
       platform — non-error skip)
    1  spawn / capture / parse failure (logged for diagnosis)
"""
import os, sys, subprocess, time, traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _budget_core import (
    record_observed_pct, scan_window, load_calibration, save_calibration,
    AUTO_CAL_ENABLED, auto_calibrate_supported,
)
from calibrate import parse_usage_text


LOG_PATH = os.path.expanduser("~/.claude/.budget_auto_calibrate.log")
LOG_MAX_BYTES = 256 * 1024   # ~256KB; truncate-on-rotate keeps it tiny

SPAWN_TIMEOUT_SECS = float(os.environ.get("BUDGET_AUTO_CAL_TIMEOUT_SECS", "30"))
PROMPT_SETTLE_SECS = float(os.environ.get("BUDGET_AUTO_CAL_SETTLE_SECS", "0.6"))
PANEL_SETTLE_SECS = float(os.environ.get("BUDGET_AUTO_CAL_PANEL_SETTLE_SECS", "0.6"))

# UTF-8 byte sequences for the TUI landmarks we wait on.
PROMPT_MARKER = "❯".encode("utf-8")    # input arrow — TUI ready for keystroke
PANEL_MARKER = b"Resets"               # /usage panel rendered (Current session row)


def _log(msg):
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            try:
                os.rename(LOG_PATH, LOG_PATH + ".old")
            except OSError:
                pass
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass


# --------------------------------------------------------------------------
# PTY adapter — single interface, two backends. Both expose:
#   read(n=4096, timeout=0.5) -> bytes (may be b"" on timeout / EOF)
#   write(data: bytes) -> None
#   close() -> None  (idempotent; always safe to call twice)
# --------------------------------------------------------------------------

class _PosixPty:
    """stdlib `pty` + subprocess.Popen. Master fd is select()-pollable."""
    def __init__(self, cmd, env):
        import pty, select  # POSIX-only — imported lazily so Windows can
        import signal       # import this module without crashing
        self._pty = pty
        self._select = select
        self._signal = signal

        master, slave = pty.openpty()
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=slave, stdout=slave, stderr=slave,
                close_fds=True, env=env, start_new_session=True,
            )
        except Exception:
            os.close(master)
            os.close(slave)
            raise
        os.close(slave)
        self._master = master

    def read(self, n=4096, timeout=0.5):
        rlist, _, _ = self._select.select([self._master], [], [], timeout)
        if not rlist:
            return b""
        try:
            return os.read(self._master, n)
        except OSError:
            return b""

    def write(self, data):
        try:
            os.write(self._master, data)
        except OSError:
            pass

    def close(self):
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=2)
        except Exception:
            try:
                os.killpg(os.getpgid(self._proc.pid), self._signal.SIGKILL)
            except Exception:
                pass
        try:
            os.close(self._master)
        except OSError:
            pass


class _WindowsPty:
    """pywinpty + reader thread + queue.

    pywinpty's `read()` is blocking with no timeout primitive, so a
    background reader pumps bytes into a queue that the main loop drains
    with `Queue.get(timeout=...)`. Same select-style polling shape as the
    POSIX adapter from the caller's perspective."""
    def __init__(self, cmd, env):
        from winpty import PtyProcess  # third-party; install via pip
        import threading, queue
        self._queue = queue.Queue()
        self._queue_empty_cls = queue.Empty
        # winpty wants list-of-strings; subprocess-style cmd works.
        self._proc = PtyProcess.spawn(cmd, env=env, dimensions=(40, 120))
        self._stopped = False

        def _pump():
            while not self._stopped:
                try:
                    chunk = self._proc.read(4096)
                except (EOFError, OSError):
                    break
                except Exception:
                    break
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="ignore")
                self._queue.put(chunk)
        self._thread = threading.Thread(target=_pump, daemon=True)
        self._thread.start()

    def read(self, n=4096, timeout=0.5):
        try:
            return self._queue.get(timeout=timeout)
        except self._queue_empty_cls:
            return b""

    def write(self, data):
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="ignore")
        try:
            self._proc.write(data)
        except Exception:
            pass

    def close(self):
        self._stopped = True
        try:
            self._proc.terminate(force=True)
        except Exception:
            pass


def _open_pty(cmd, env):
    """Return a backend appropriate to the current OS. Caller must call
    `.close()`. Raises ImportError on Windows when pywinpty isn't
    installed; the hook gates on `auto_calibrate_supported()` first so
    we shouldn't normally reach here without a working backend."""
    if sys.platform == "win32":
        return _WindowsPty(cmd, env)
    return _PosixPty(cmd, env)


# --------------------------------------------------------------------------
# Capture flow
# --------------------------------------------------------------------------

def spawn_claude_capture_usage(timeout=SPAWN_TIMEOUT_SECS):
    """Spawn claude under the platform's pty, drive /usage, return (text, ok).

    `ok=True` means we observed both the prompt and the panel landmarks
    before timeout. Whatever bytes we read are returned regardless so the
    caller can log a snippet on failure."""
    env = dict(os.environ)
    env["BUDGET_AUTO_CALIBRATE_RUNNING"] = "1"
    env.setdefault("TERM", "xterm-256color")

    try:
        ptp = _open_pty(["claude"], env)
    except ImportError as e:
        _log(f"pty backend unavailable: {e}")
        return "", False
    except FileNotFoundError:
        _log("claude binary not on PATH — cannot auto-calibrate")
        return "", False
    except Exception:
        _log(f"pty open failed: {traceback.format_exc()}")
        return "", False

    output = bytearray()
    start = time.time()
    deadline = start + timeout
    state = "wait_prompt"   # wait_prompt → wait_panel → done
    sent_usage_at = 0.0

    try:
        while time.time() < deadline:
            chunk = ptp.read(n=4096, timeout=0.5)
            if chunk:
                output.extend(chunk)

            if state == "wait_prompt" and PROMPT_MARKER in output:
                time.sleep(PROMPT_SETTLE_SECS)
                ptp.write(b"/usage\r")
                sent_usage_at = time.time()
                state = "wait_panel"
                continue

            if state == "wait_panel" and PANEL_MARKER in output:
                time.sleep(PANEL_SETTLE_SECS)
                ptp.write(b"\x1b")          # ESC dismisses
                time.sleep(0.2)
                state = "done"
                break

            # Defensive: if we sent /usage but never see PANEL_MARKER,
            # bail after 10s of waiting post-send so the parent doesn't
            # hang on an unexpected TUI state.
            if state == "wait_panel" and time.time() - sent_usage_at > 10:
                break
    finally:
        ptp.close()

    if state != "done":
        # Most common failure mode is TUI drift: Claude CLI output no
        # longer contains PROMPT_MARKER / PANEL_MARKER as-is. Log enough
        # to diagnose without re-running, plus the manual fallback path.
        saw_prompt = PROMPT_MARKER in output
        saw_panel = PANEL_MARKER in output
        elapsed = time.time() - start
        _log(
            f"capture incomplete: state={state} saw_prompt={saw_prompt} "
            f"saw_panel={saw_panel} bytes={len(output)} elapsed={elapsed:.1f}s "
            f"timeout={timeout:.1f}s. "
            f"If the Claude CLI TUI changed, PROMPT_MARKER={PROMPT_MARKER!r} "
            f"and/or PANEL_MARKER={PANEL_MARKER!r} may no longer match. "
            f"Fallback: run `calibrate.py --observed-pct <N>` manually. "
            f"Output tail: {bytes(output[-400:])!r}"
        )

    text = output.decode("utf-8", errors="ignore")
    return text, state == "done"


# --------------------------------------------------------------------------
# Entry
# --------------------------------------------------------------------------

def main():
    if not AUTO_CAL_ENABLED:
        _log("BUDGET_AUTO_CAL_ENABLED=0 — exiting")
        return 0
    if not auto_calibrate_supported():
        _log(f"platform {sys.platform} not supported (no pty/pywinpty) — exiting")
        return 0
    if os.environ.get("BUDGET_AUTO_CALIBRATE_RUNNING") == "1":
        _log("recursion guard tripped — exiting")
        return 0

    # Cooldown / milestone gating is enforced by the dispatcher (the hook).
    # When this script is invoked we just run; the hook only spawns us when
    # a fresh dispatch is warranted.
    now = time.time()
    _log(f"spawning claude /usage on {sys.platform}")
    try:
        text, ok = spawn_claude_capture_usage()
    except Exception:
        _log(f"spawn raised: {traceback.format_exc()}")
        return 1

    if not ok:
        # spawn_claude_capture_usage already logged the detailed failure mode.
        return 1

    pct, reset = parse_usage_text(text)
    if pct is None:
        _log(f"parse failed; len(text)={len(text)} snippet={text[-400:]!r}")
        return 1

    weighted, oldest, _ = scan_window(now=now)
    if weighted == 0:
        _log("no weighted usage in 5h window — nothing to calibrate against")
        return 1

    pct_clamped = min(pct, 100.0)
    cal_pre = load_calibration()
    prior_limit = (cal_pre.get("limit") if isinstance(cal_pre.get("limit"), (int, float))
                   else None)
    new_limit = record_observed_pct(pct_clamped, weighted=weighted)

    cal = load_calibration()
    state = cal.get("auto_cal_state") or {}
    state["last_success_ts"] = now
    state["last_observed_pct"] = pct_clamped
    state["last_reset_clue"] = reset
    cal["auto_cal_state"] = state
    save_calibration(cal)

    _log(
        f"calibrated: pct={pct_clamped:.1f}% reset={reset!r} "
        f"weighted={weighted:,} prior_limit={prior_limit} new_limit={new_limit:,}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
