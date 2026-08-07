"""
WhisperLocal — the macOS app bundle.

Installing a command-line tool is not enough to make this usable. Run from a
terminal, dictation dies when that terminal closes, does not come back after a
reboot, and — worst of all — macOS attaches the Accessibility and Input
Monitoring grants to the *terminal* rather than to WhisperLocal.

So we build a real app bundle: `~/Applications/WhisperLocal.app`. Launch
Services starts it at login, which makes the bundle the TCC "responsible
process". The Python child inherits that identity, so the permissions live on
"WhisperLocal" and survive reboots, terminal restarts and `brew upgrade python`.

    whisperlocal install-app      build/repair it, register it, launch it
    whisperlocal uninstall-app    remove it

── The rule that governs this whole file ────────────────────────────────────

macOS keys the TCC grants to the bundle's **cdhash**, which is sealed from
exactly two files: `Contents/Info.plist` and `Contents/MacOS/WhisperLocal`.

**Those two files must be byte-identical for every user and every release.**
If a version number or an absolute path leaked into either one, the cdhash
would change on every upgrade and silently revoke the user's permissions,
leaving them with an app that looks fine and does nothing.

That is why the launcher below contains no version string and no path that is
not derived from `$HOME` at runtime, and why everything that legitimately
changes between releases lives in `start.sh`, *outside* the bundle. It is also
why `install_app()` compares before it writes and never re-signs a bundle whose
content already matches.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

APP_NAME = "WhisperLocal"
BUNDLE_ID = "com.shivamdixit.whisperlocal"

# ─── Sealed bundle content — see the module docstring before editing ─────────────
# Any change to these two strings changes the cdhash and revokes every existing
# user's Accessibility and Input Monitoring grants. Treat them as frozen.

INFO_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>
  <string>WhisperLocal</string>

  <key>CFBundleDisplayName</key>
  <string>WhisperLocal</string>

  <!-- TCC identity. Never change this: the Accessibility / Input Monitoring /
       Microphone grants are keyed to it. -->
  <key>CFBundleIdentifier</key>
  <string>com.shivamdixit.whisperlocal</string>

  <key>CFBundleExecutable</key>
  <string>WhisperLocal</string>

  <key>CFBundlePackageType</key>
  <string>APPL</string>

  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>

  <key>CFBundleShortVersionString</key>
  <string>1.0</string>

  <key>CFBundleVersion</key>
  <string>1</string>

  <!-- Menu-bar-only app: no Dock icon, no app switcher entry. -->
  <key>LSUIElement</key>
  <true/>

  <key>LSMinimumSystemVersion</key>
  <string>13.0</string>

  <key>NSMicrophoneUsageDescription</key>
  <string>WhisperLocal records your voice while you hold the push-to-talk key so it can transcribe it locally on this Mac.</string>

  <key>NSHighResolutionCapable</key>
  <true/>
</dict>
</plist>
"""

LAUNCHER = """\
#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# WhisperLocal.app — launcher
# ──────────────────────────────────────────────────────────────────────────────
# Launch Services starts this bundle at login, which makes the bundle the TCC
# "responsible process". The python child inherits that identity, so the
# Accessibility / Input Monitoring / Microphone grants live on "WhisperLocal"
# and survive reboots and `brew upgrade python`.
#
# Everything the app prints goes to ~/Library/Logs/WhisperLocal.log — that is
# the only diagnostic once there is no terminal attached.
# ──────────────────────────────────────────────────────────────────────────────

APP_DIR="$HOME/Applications/WhisperLocal"
LOG="$HOME/Library/Logs/WhisperLocal.log"

mkdir -p "$(dirname "$LOG")"

# Keep the log from growing without bound across logins.
if [ -f "$LOG" ] && [ "$(stat -f%z "$LOG")" -gt 1048576 ]; then
    mv "$LOG" "$LOG.1"
fi

{
    echo ""
    echo "═══ WhisperLocal starting: $(date) ═══"
} >> "$LOG" 2>&1

# Homebrew python and ffmpeg are not on launchd's default PATH.
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Without this, python block-buffers stdout when it is a file rather than a tty
# and the log stays empty until the process exits.
export PYTHONUNBUFFERED=1

exec "$APP_DIR/start.sh" >> "$LOG" 2>&1
"""

# ─── Unsealed: freely rewritten on every upgrade ─────────────────────────────────
# This lives outside the bundle precisely so it can change without touching the
# cdhash. All the version-specific and path-specific logic belongs here.

SUPERVISOR = """\
#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# WhisperLocal — supervised launcher
# ──────────────────────────────────────────────────────────────────────────────
# Restarts the app if it dies unexpectedly, so a crash doesn't silently leave
# you with no dictation until you happen to notice the menu bar icon is gone.
#
# Exit 0 (menu bar → Quit) stops the loop. Anything else is treated as a crash
# and retried. A burst of rapid failures gives up rather than spinning forever.
#
# This file lives OUTSIDE WhisperLocal.app on purpose. The bundle is ad-hoc
# signed and TCC keys the Accessibility / Input Monitoring grants to its
# cdhash, so editing anything inside it would force a re-sign and could revoke
# those permissions. Everything that changes between releases lives here.
#
# Regenerated by `whisperlocal install-app` — local edits will be overwritten.
# ──────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

MAX_FAILURES=5      # give up after this many crashes...
FAILURE_WINDOW=60   # ...within this many seconds
RESTART_DELAY=3

# Find the interpreter that has whisperlocal installed. A venv beside this
# script wins, so an existing hand-built setup keeps working; otherwise fall
# back to the uv tool environment the installer creates.
RUN=""
for candidate in \\
    "$SCRIPT_DIR/venv/bin/python3" \\
    "$HOME/.local/share/uv/tools/whisperlocal/bin/python3"
do
    if [ -x "$candidate" ] && "$candidate" -c "import whisperlocal" 2>/dev/null; then
        RUN="$candidate -m whisperlocal"
        break
    fi
done

# Last resort: the console script on PATH.
if [ -z "$RUN" ]; then
    for candidate in "$HOME/.local/bin/whisperlocal" "$(command -v whisperlocal 2>/dev/null)"; do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            RUN="$candidate"
            break
        fi
    done
fi

if [ -z "$RUN" ]; then
    echo "Error: cannot find a Python with whisperlocal installed."
    echo "   Reinstall with:"
    echo "     curl -fsSL https://raw.githubusercontent.com/shivamdixit17/whisperlocal/main/install.sh | bash"
    exit 1
fi

echo "Running: $RUN"

failures=0
window_start=$(date +%s)

while true; do
    $RUN
    exit_code=$?

    if [ $exit_code -eq 0 ]; then
        echo "WhisperLocal exited cleanly — not restarting."
        break
    fi

    now=$(date +%s)
    if [ $((now - window_start)) -gt $FAILURE_WINDOW ]; then
        # Outside the window — start counting again.
        failures=0
        window_start=$now
    fi
    failures=$((failures + 1))

    if [ $failures -ge $MAX_FAILURES ]; then
        echo "WhisperLocal crashed $failures times in under ${FAILURE_WINDOW}s (last exit: $exit_code)."
        echo "   Giving up — this is a real bug, not a blip. Check the crash report:"
        echo "   ls -t ~/Library/Logs/DiagnosticReports/Python*.ips | head -1"
        exit 1
    fi

    echo "WhisperLocal exited with code $exit_code at $(date) — restart $failures/$MAX_FAILURES in ${RESTART_DELAY}s"
    sleep $RESTART_DELAY
done
"""


# ─── Paths ───────────────────────────────────────────────────────────────────────


def applications_dir() -> Path:
    return Path.home() / "Applications"


def bundle_path() -> Path:
    return applications_dir() / f"{APP_NAME}.app"


def support_dir() -> Path:
    """Where the supervisor lives — beside the bundle, but not inside it."""
    return applications_dir() / APP_NAME


def supervisor_path() -> Path:
    return support_dir() / "start.sh"


def log_path() -> Path:
    return Path.home() / "Library" / "Logs" / f"{APP_NAME}.log"


# ─── Bundle ──────────────────────────────────────────────────────────────────────


def _sealed_files() -> dict[Path, str]:
    """The two files that make up the cdhash, and their required content."""
    bundle = bundle_path()
    return {
        bundle / "Contents" / "Info.plist": INFO_PLIST,
        bundle / "Contents" / "MacOS" / APP_NAME: LAUNCHER,
    }


def bundle_is_current() -> bool:
    """
    True if every sealed file already holds exactly the bytes we would write.

    This is the check that keeps upgrades from revoking permissions: when it
    passes, we do not touch the bundle and the cdhash cannot change.
    """
    for path, expected in _sealed_files().items():
        try:
            if path.read_text(encoding="utf-8") != expected:
                return False
        except (OSError, UnicodeDecodeError):
            return False
    return True


def bundle_cdhash() -> str | None:
    """The bundle's current cdhash, or None if it is missing or unsigned."""
    if not bundle_path().exists():
        return None
    try:
        result = subprocess.run(
            ["codesign", "-dvvv", str(bundle_path())],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    for line in (result.stderr or "").splitlines():
        if line.startswith("CDHash="):
            return line.split("=", 1)[1].strip()
    return None


def write_bundle(force: bool = False) -> bool:
    """
    Create or repair the bundle. Returns True if anything was written.

    Writing is skipped entirely when the content already matches, so a reinstall
    or upgrade leaves the signature — and the user's granted permissions — alone.
    """
    if bundle_is_current() and not force:
        return False

    bundle = bundle_path()
    (bundle / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)

    for path, content in _sealed_files().items():
        path.write_text(content, encoding="utf-8")

    launcher = bundle / "Contents" / "MacOS" / APP_NAME
    launcher.chmod(0o755)

    # Ad-hoc signature. The app is not distributed as a download, so it never
    # picks up a quarantine attribute and Gatekeeper never inspects it; the
    # signature exists to give TCC a stable identity to attach grants to.
    subprocess.run(
        ["codesign", "--force", "--sign", "-", str(bundle)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return True


def write_supervisor() -> None:
    """(Re)write the launcher script. Always safe — it is outside the seal."""
    path = supervisor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SUPERVISOR, encoding="utf-8")
    path.chmod(0o755)


# ─── Login item ──────────────────────────────────────────────────────────────────


def _osascript(script: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return result.returncode == 0, (result.stdout or result.stderr or "").strip()


def login_item_exists() -> bool:
    ok, out = _osascript(
        'tell application "System Events" to get the name of every login item'
    )
    return ok and APP_NAME in out


def register_login_item() -> bool:
    """Start WhisperLocal at login. Idempotent."""
    if login_item_exists():
        return True
    ok, _ = _osascript(
        'tell application "System Events" to make login item at end with properties '
        f'{{path:"{bundle_path()}", hidden:false}}'
    )
    return ok


def unregister_login_item() -> bool:
    if not login_item_exists():
        return True
    ok, _ = _osascript(
        f'tell application "System Events" to delete login item "{APP_NAME}"'
    )
    return ok


# ─── Process control ─────────────────────────────────────────────────────────────


def is_running() -> bool:
    """True if the supervisor or an orphaned app process is alive."""
    return bool(
        _pids_matching(str(supervisor_path())) or _pids_matching(APP_PROCESS_PATTERN)
    )


def launch() -> bool:
    """
    Start the app through the bundle.

    `open -a` matters: launching start.sh directly would make the terminal the
    TCC responsible process, which is the whole problem the bundle exists to
    solve.
    """
    result = subprocess.run(
        ["open", "-a", str(bundle_path())], capture_output=True, text=True
    )
    return result.returncode == 0


# How the supervisor invokes the app. Bracketed so the argument does not begin
# with a dash: `pgrep -f "-m whisperlocal"` parses the leading "-m" as an option
# and matches nothing at all, silently, which leaves the old process running and
# ends up with two Fn listeners after a restart.
APP_PROCESS_PATTERN = "[-]m whisperlocal"


def _pids_matching(pattern: str) -> list[int]:
    """PIDs whose command line matches, never including our own."""
    result = subprocess.run(
        ["pgrep", "-f", "--", pattern], capture_output=True, text=True
    )
    me = os.getpid()
    pids = []
    for token in result.stdout.split():
        try:
            pid = int(token)
        except ValueError:
            continue
        if pid != me:
            pids.append(pid)
    return pids


def quit_running() -> None:
    """
    Stop any running instance.

    The supervisor goes first — kill the Python child while its parent is still
    alive and the parent dutifully restarts it. Then the child itself, matched
    on `-m whisperlocal`, which is how the supervisor launches it.

    Killing the supervisor alone is not enough: it leaves an orphaned child
    holding the Fn event tap, so a restart ends up with two listeners and every
    key press recorded twice.
    """
    for pattern in (str(supervisor_path()), APP_PROCESS_PATTERN):
        for pid in _pids_matching(pattern):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

    # Give them a moment, then insist.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not any(_pids_matching(p) for p in (str(supervisor_path()), APP_PROCESS_PATTERN)):
            return
        time.sleep(0.2)

    for pattern in (str(supervisor_path()), APP_PROCESS_PATTERN):
        for pid in _pids_matching(pattern):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


# ─── Commands ────────────────────────────────────────────────────────────────────


def install(force: bool = False, start: bool = True) -> int:
    """Build/repair the bundle, register it for login, and launch it."""
    if sys.platform != "darwin":
        print("Error: the app bundle is macOS only.")
        return 1

    before = bundle_cdhash()

    if write_bundle(force=force):
        action = "Rebuilt" if before else "Created"
        print(f"{action} {bundle_path()}")
    else:
        print(f"App bundle already up to date: {bundle_path()}")
        print("   Left untouched so your granted permissions survive.")

    after = bundle_cdhash()
    if before and after and before != after:
        print()
        print("Note: the bundle signature changed, so macOS has forgotten its")
        print("      Accessibility and Input Monitoring grants. Re-grant them:")
        print("      whisperlocal doctor")
        print()

    write_supervisor()
    print(f"Wrote {supervisor_path()}")

    if register_login_item():
        print("Starts automatically at login")
    else:
        print("Warning: could not register the login item.")
        print("   Add it yourself: System Settings > General > Login Items")
        print(f"   The app is at {bundle_path()}")

    if start:
        if is_running():
            print("Restarting...")
            quit_running()
        if launch():
            print("Launched — look for the icon in your menu bar")
        else:
            print(f"Warning: could not launch it. Open {bundle_path()} yourself.")

    print()
    print(f"Logs: {log_path()}")
    return 0


def uninstall() -> int:
    """Remove the bundle, the login item and the supervisor."""
    quit_running()

    if unregister_login_item():
        print("Removed the login item")

    if bundle_path().exists():
        shutil.rmtree(bundle_path(), ignore_errors=True)
        print(f"Removed {bundle_path()}")

    if supervisor_path().exists():
        supervisor_path().unlink()
        print(f"Removed {supervisor_path()}")
        # Only clear the directory if nothing else of the user's is in it.
        try:
            os.rmdir(support_dir())
        except OSError:
            pass

    print()
    print("macOS keeps the stale Accessibility / Input Monitoring entries until")
    print("you remove them by hand in System Settings > Privacy & Security.")
    return 0
