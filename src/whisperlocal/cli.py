"""
WhisperLocal — command line entry point.

    whisperlocal            start the menu bar app
    whisperlocal doctor     check that everything is set up correctly
    whisperlocal config     create, locate or print your settings
    whisperlocal stats      summarise your dictation history
    whisperlocal dashboard  open the analytics dashboard and settings page
    whisperlocal api-key    store, check or remove the cloud transcription key
    whisperlocal meetings   list, show, search and export recorded meetings

    whisperlocal install-app    install the menu bar app (starts at login)
    whisperlocal uninstall-app  remove it
    whisperlocal probe-caret    check if an app reports its text cursor
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from whisperlocal import __version__
from whisperlocal import config as cfg

# System Settings panes we point people at, as deep links macOS understands.
PANE = "x-apple.systempreferences:com.apple.preference.security"
PERMISSION_PANES = {
    "Accessibility": f"{PANE}?Privacy_Accessibility",
    "Microphone": f"{PANE}?Privacy_Microphone",
    "Input Monitoring": f"{PANE}?Privacy_ListenEvent",
    "System Audio Recording": f"{PANE}?Privacy_AudioCapture",
}

OK = "OK  "
WARN = "warn"
FAIL = "FAIL"


# ─── Platform guard ──────────────────────────────────────────────────────────────


def check_platform() -> str | None:
    """
    Return a human-readable reason this machine cannot run WhisperLocal, or
    None if it can. Checked before any heavy import so an unsupported machine
    gets an explanation instead of an MLX stack trace.
    """
    if sys.platform != "darwin":
        return (
            "WhisperLocal only runs on macOS. It is built on MLX (Apple's "
            "framework for Apple Silicon) and on macOS-specific APIs for the "
            "menu bar, the Fn key and the global hotkey."
        )
    if platform.machine() != "arm64":
        return (
            "WhisperLocal needs an Apple Silicon Mac (M1 or newer). This Mac "
            f"reports its architecture as {platform.machine()!r}, so MLX cannot "
            "run here.\n"
            "   If you are on Apple Silicon but running Python under Rosetta, "
            "reinstall Python as a native arm64 build."
        )
    return None


# ─── doctor ──────────────────────────────────────────────────────────────────────


def _check_ffmpeg() -> tuple[bool, str]:
    """mlx-whisper shells out to ffmpeg to decode audio."""
    if shutil.which("ffmpeg"):
        return True, "ffmpeg found"
    return False, "ffmpeg is missing — install it with: brew install ffmpeg"


def _check_microphone() -> tuple[bool, str]:
    """Confirm there is an input device we could actually record from."""
    try:
        import sounddevice
    except Exception as exc:
        return False, f"could not load sounddevice ({exc})"

    try:
        device = sounddevice.query_devices(kind="input")
        rate = int(device.get("default_samplerate") or 0)
        return True, f"input device: {device['name']} ({rate} Hz)"
    except Exception as exc:
        return False, f"no usable microphone ({exc})"


def _check_accessibility() -> tuple[bool, str]:
    """
    Accessibility is what lets us press Cmd+V for you. The permission belongs to
    whichever app launched this process — usually your terminal.
    """
    try:
        from ApplicationServices import AXIsProcessTrusted
    except ImportError:
        return False, "could not check (PyObjC ApplicationServices missing)"

    if AXIsProcessTrusted():
        return True, "Accessibility granted"
    return False, (
        "Accessibility not granted to the app running this command.\n"
        "      Transcriptions will be copied to your clipboard but not pasted.\n"
        f"      Grant it here: {PERMISSION_PANES['Accessibility']}"
    )


def _check_event_tap(settings: cfg.Settings) -> tuple[bool, str] | None:
    """The Fn trigger needs a Quartz event tap, which needs Input Monitoring."""
    if not settings.uses_fn:
        return None
    try:
        from Quartz import (
            CGEventMaskBit,
            CGEventTapCreate,
            kCGEventFlagsChanged,
            kCGEventTapOptionListenOnly,
            kCGHeadInsertEventTap,
            kCGSessionEventTap,
        )
    except ImportError:
        return False, "Quartz is unavailable — the Fn trigger cannot work"

    tap = CGEventTapCreate(
        kCGSessionEventTap,
        kCGHeadInsertEventTap,
        kCGEventTapOptionListenOnly,
        CGEventMaskBit(kCGEventFlagsChanged),
        lambda *a: None,
        None,
    )
    if tap:
        return True, "Fn event tap can be created"
    return False, (
        "cannot create the Fn event tap — the Fn key will never trigger.\n"
        "      Grant Input Monitoring to your terminal, then restart it:\n"
        f"      {PERMISSION_PANES['Input Monitoring']}"
    )


def _check_model_cached(settings: cfg.Settings) -> tuple[bool, str]:
    """Report whether the selected model still needs downloading."""
    org, _, name = settings.model_path.partition("/")
    hub = Path.home() / ".cache" / "huggingface" / "hub"
    if hub.is_dir() and any(hub.glob(f"models--{org}--{name}")):
        return True, f"model '{settings.model}' is downloaded"
    return False, (
        f"model '{settings.model}' is not downloaded yet — "
        "it will download on your first dictation"
    )


def _check_memory(settings: cfg.Settings) -> tuple[bool, str] | None:
    """
    Report what the running app is actually using.

    macOS reports "memory" as phys_footprint, which is what Activity Monitor
    shows and is far larger than the resident size — most of an idle app's
    footprint is compressed rather than resident.
    """
    from whisperlocal import appbundle

    pids = appbundle._pids_matching(appbundle.APP_PROCESS_PATTERN)
    if not pids:
        return None

    try:
        out = subprocess.run(
            ["footprint", "-p", str(pids[0])], capture_output=True, text=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None

    for line in out.splitlines():
        if "phys_footprint:" in line and "peak" not in line:
            used = line.split(":", 1)[1].strip()
            limit = settings.mlx_cache_mb
            note = "no cache limit" if limit < 0 else f"cache capped at {limit} MB"
            return True, f"memory: {used} ({note})"
    return None


def _check_meetings(settings: cfg.Settings) -> list[tuple[bool, str]]:
    """System-audio capture and the meetings folder."""
    out: list[tuple[bool, str]] = []
    if not settings.meeting_enabled:
        out.append((True, "meetings: detection off (meeting_enabled = false)"))
        return out
    try:
        from whisperlocal import systemaudio

        ok, why = systemaudio.is_available()
    except Exception as exc:  # pragma: no cover
        ok, why = False, str(exc)
    if ok:
        out.append((True, "meetings: system-audio tap available (macOS asks the first time it is used)"))
    else:
        out.append((False, f"meetings: microphone only — {why}"))
    root = settings.meeting_root
    try:
        root.mkdir(parents=True, exist_ok=True)
        count = sum(1 for p in root.iterdir() if (p / "meeting.json").exists())
        out.append((True, f"meetings: {count} recorded, in {root}"))
    except OSError as exc:
        out.append((False, f"meetings: cannot write {root}: {exc}"))
    return out


def _check_api(settings: cfg.Settings) -> list[tuple[bool, str]]:
    """Only relevant once a backend is set to the cloud API."""
    if not settings.uses_api:
        return [(True, "backend: local (no network)")]
    from whisperlocal import keychain

    out = []
    if keychain.has_api_key():
        out.append((True, f"backend: api — key present, {settings.api_base_url} · {settings.api_model}"))
    else:
        out.append((False, "backend: api selected but no key — run: whisperlocal api-key set"))
    try:
        enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=10)
        if " aac " in enc.stdout:
            out.append((True, "ffmpeg: aac encoder available for uploads"))
        else:
            out.append((False, "ffmpeg: no aac encoder — uploads will fail"))
    except Exception:
        pass
    return out


def cmd_doctor(args: argparse.Namespace) -> int:
    """Run every check and summarize what, if anything, needs fixing."""
    print(f"WhisperLocal {__version__} — checking your setup\n")

    problem = check_platform()
    if problem:
        print(f"[{FAIL}] {problem}")
        return 1

    print(f"[{OK}] macOS on Apple Silicon ({platform.mac_ver()[0] or 'unknown version'})")
    print(f"[{OK}] Python {platform.python_version()}")

    settings = cfg.load()
    path = cfg.config_path()
    if path.exists():
        print(f"[{OK}] config: {path}")
    else:
        print(f"[{OK}] config: defaults (run 'whisperlocal config --init' to change)")
    print(f"[{OK}] trigger: {settings.trigger_label}")

    # Hard requirements — these stop dictation working at all.
    failures = 0
    checks = [_check_ffmpeg(), _check_microphone()]
    tap = _check_event_tap(settings)
    if tap:
        checks.append(tap)

    for ok, message in checks:
        print(f"[{OK if ok else FAIL}] {message}")
        failures += 0 if ok else 1

    # Soft checks — degraded but usable.
    soft = [_check_accessibility(), _check_model_cached(settings)]
    memory = _check_memory(settings)
    if memory:
        soft.append(memory)
    soft.extend(_check_meetings(settings))
    soft.extend(_check_api(settings))
    for ok, message in soft:
        print(f"[{OK if ok else WARN}] {message}")

    if settings.history_enabled:
        hp = settings.history_path
        kind = "text + stats" if settings.history_text else "stats only"
        if hp.exists():
            with hp.open(encoding="utf-8") as fh:
                count = sum(1 for line in fh if line.strip())
            state = f"{count} entries"
        else:
            state = "no entries yet"
        print(f"[{OK}] history: {kind}, {state}")
        print(f"        {hp}")

    if settings.web_enabled:
        print(f"[{OK}] dashboard: http://127.0.0.1:{settings.web_port} (open it from the menu bar)")

    print()
    print("These permissions belong to the app that launches WhisperLocal")
    print("(your terminal, or whatever wrapper you use). Grant them here:")
    for label, link in PERMISSION_PANES.items():
        print(f"   - {label:<17} {link}")

    print()
    if failures:
        print(f"[{FAIL}] {failures} problem(s) to fix before dictation will work.")
        return 1

    print(f"[{OK}] Ready. Start it with: whisperlocal")
    print(f"        Then hold {settings.trigger_label} and speak.")
    return 0


# ─── config ──────────────────────────────────────────────────────────────────────


def cmd_config(args: argparse.Namespace) -> int:
    """Create, locate or print the configuration."""
    path = cfg.config_path()

    if args.path:
        print(path)
        return 0

    if args.init:
        if path.exists() and not args.force:
            print(f"[{WARN}] {path} already exists. Use --force to overwrite it.")
            return 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cfg.TEMPLATE, encoding="utf-8")
        print(f"[{OK}] Wrote {path}")
        print("   Open it in your editor and change whatever you like.")
        return 0

    if args.show:
        try:
            settings = cfg.load(strict=True)
        except cfg.ConfigError as exc:
            print(f"[{FAIL}] {exc}")
            print(f"   Fix it in {path}")
            return 1

        source = str(path) if path.exists() else "built-in defaults"
        print(f"Effective settings (from {source}):\n")
        width = max(len(f) for f in vars(settings))
        for key, value in vars(settings).items():
            env = f"{cfg.ENV_PREFIX}{key.upper()}"
            shown = ",".join(str(v) for v in value) if isinstance(value, tuple) else str(value)
            print(f"  {key:<{width}}  {shown:<26}  ({env})")
        return 0

    if path.exists():
        print(f"Config file: {path}")
        print("  whisperlocal config --show    print the effective settings")
    else:
        print("No config file yet. WhisperLocal is running on its defaults.")
        print(f"  whisperlocal config --init    create one at {path}")
        print("  whisperlocal config --show    print the effective settings")
    return 0


# ─── stats ───────────────────────────────────────────────────────────────────────


def cmd_stats(args: argparse.Namespace) -> int:
    """Summarise the dictation history."""
    from whisperlocal import stats

    settings = cfg.load()
    path = Path(args.file).expanduser() if args.file else None

    if getattr(args, "json", False):
        import json

        from whisperlocal import analytics

        source = path or settings.history_path
        entries, skipped = stats.load_entries(source, args.days) if source.exists() else ([], 0)
        data = analytics.build_dashboard(entries, days=args.days)
        data["skipped"] = skipped
        print(json.dumps(data, indent=2))
        return 0

    if getattr(args, "meetings", False):
        import json

        from whisperlocal.meetings import MeetingStore

        data = MeetingStore(settings.meeting_root).stats(days=args.days)
        print(json.dumps(data, indent=2))
        return 0

    return stats.report(settings, days=args.days, show_text=args.text, path=path)


# ─── dashboard ───────────────────────────────────────────────────────────────────


class StandaloneContext:
    """What the web layer sees when the menu bar app is not running.

    Settings still save (they apply on the next launch); analytics and
    meetings are read-only; anything that needs the live engine says so.
    """

    running = False

    def __init__(self, settings_mgr):
        from whisperlocal import keychain

        self.settings = settings_mgr
        self.version = __version__
        self._keychain = keychain
        self.recorder = None
        try:
            from whisperlocal.meetings import MeetingStore

            self.meetings = MeetingStore(settings_mgr.current.meeting_root)
        except Exception:
            self.meetings = None

    @property
    def history_path(self):
        return self.settings.current.history_path

    def status(self) -> dict:
        return {"running": False}

    def _unsupported(self, *_args):
        from whisperlocal.web.api import NotSupported

        raise NotSupported("WhisperLocal is not running — start it to use this")

    start_capture = capture_state = cancel_capture = request_restart = _unsupported

    def api_key_set(self) -> bool:
        return self._keychain.has_api_key()

    def set_api_key(self, key: str) -> None:
        self._keychain.set_api_key(key)

    def clear_api_key(self) -> None:
        self._keychain.clear_api_key()


def _open_in_browser(url: str) -> None:
    subprocess.Popen(["open", url])


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Open the dashboard: the running app's if there is one, else a standalone server."""
    from whisperlocal.settings_manager import SettingsManager
    from whisperlocal.web.server import WebConfig, WebServer, ping, read_discovery

    tab = args.tab or "dashboard"
    live = read_discovery()
    if live and ping(live.get("url", "")):
        url = f"{live['url'].rstrip('/')}/?token={live['token']}#{tab}"
        print(f"WhisperLocal is running — opening {live['url']}")
        if not args.no_browser:
            _open_in_browser(url)
        return 0

    settings = cfg.load()
    server = WebServer(StandaloneContext(SettingsManager(settings)), WebConfig(port=settings.web_port))
    base = server.start()
    print("WhisperLocal is not running; serving the dashboard on its own.")
    print(f"   {base}")
    print("   Settings saved here apply the next time the app starts. Ctrl-C to stop.")
    if not args.no_browser:
        _open_in_browser(server.url(tab))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped")
    finally:
        server.stop()
    return 0


# ─── api-key ─────────────────────────────────────────────────────────────────────


def cmd_api_key(args: argparse.Namespace) -> int:
    """Manage the cloud transcription key in the macOS Keychain."""
    from whisperlocal import keychain

    if args.action == "set":
        import getpass

        key = getpass.getpass("API key (input hidden): ").strip()
        if not key:
            print(f"[{FAIL}] Nothing entered.")
            return 1
        try:
            keychain.set_api_key(key)
        except keychain.KeychainError as exc:
            print(f"[{FAIL}] {exc}")
            return 1
        print(f"[{OK}] Stored in the Keychain (service {keychain.SERVICE!r}).")
        print("   Switch a backend to \"api\" in Settings to use it.")
        return 0

    if args.action == "clear":
        keychain.clear_api_key()
        print(f"[{OK}] Removed.")
        return 0

    if keychain.has_api_key():
        source = "environment" if keychain.ENV_VAR in __import__("os").environ else "Keychain"
        print(f"[{OK}] An API key is set ({source}).")
    else:
        print(f"[{WARN}] No API key. Store one with: whisperlocal api-key set")
    settings = cfg.load()
    print(f"   dictation_backend = {settings.dictation_backend}, meeting_backend = {settings.meeting_backend}")
    print(f"   api_base_url = {settings.api_base_url}, api_model = {settings.api_model}")
    return 0


# ─── meetings ────────────────────────────────────────────────────────────────────


def cmd_meetings(args: argparse.Namespace) -> int:
    """Inspect recorded meetings from the terminal."""
    from whisperlocal.meetings import MeetingStore

    settings = cfg.load()
    store = MeetingStore(settings.meeting_root)
    action = args.action or "list"

    if action == "list":
        page = store.list(limit=args.limit, offset=0, days=args.days)
        if not page["items"]:
            print(f"No meetings yet in {settings.meeting_root}")
            return 0
        for m in page["items"]:
            mins = (m.get("duration_s") or 0) / 60
            print(
                f"{m['id']}  {mins:5.1f} min  {m.get('words_total') or 0:6} words  "
                f"{m.get('status'):12}  {m.get('title')}"
            )
        if page["total"] > len(page["items"]):
            print(f"... {page['total'] - len(page['items'])} more (use --limit)")
        return 0

    if action == "search":
        query = args.query or args.id
        if not query:
            print(f"[{FAIL}] Give the text to search for: whisperlocal meetings search budget")
            return 2
        hits = store.search(query, limit=args.limit)
        if not hits:
            print("No matches.")
            return 0
        for h in hits:
            print(f"[{h['meeting_id']}] {h['start']:.0f}s {h['speaker']}: {h['snippet']}")
        return 0

    meeting_id = args.id
    if not meeting_id:
        print(f"[{FAIL}] A meeting id is required.")
        return 2
    if not store.exists(meeting_id):
        print(f"[{FAIL}] No meeting {meeting_id!r}.")
        return 1

    if action == "show":
        _, data, _ = store.export(meeting_id, "md")
        print(data.decode("utf-8"))
        return 0

    if action == "export":
        filename, data, _ = store.export(meeting_id, args.format)
        out = Path(args.output) if args.output else Path.cwd() / filename
        out.write_bytes(data)
        print(f"[{OK}] Wrote {out}")
        return 0

    if action == "delete":
        store.delete(meeting_id)
        print(f"[{OK}] Deleted {meeting_id}.")
        return 0

    if action == "transcribe":
        from whisperlocal.meetingrecorder import retranscribe

        return retranscribe(store, meeting_id, settings, backend_name=args.backend)

    print(f"[{FAIL}] Unknown action {action!r}.")
    return 2


# ─── app bundle ──────────────────────────────────────────────────────────────────


def cmd_install_app(args: argparse.Namespace) -> int:
    """Build the macOS app bundle so it runs without a terminal."""
    problem = check_platform()
    if problem:
        print(f"[{FAIL}] {problem}")
        return 1

    from whisperlocal import appbundle

    return appbundle.install(force=args.force, start=not args.no_start)


def cmd_uninstall_app(args: argparse.Namespace) -> int:
    """Remove the app bundle and its login item."""
    from whisperlocal import appbundle

    return appbundle.uninstall()


# ─── probe-caret ─────────────────────────────────────────────────────────────────


def cmd_probe_caret(args: argparse.Namespace) -> int:
    """
    Report what the focused app tells macOS about its text cursor.

    Cursor detection genuinely does not work everywhere, so this answers "why is
    the dot not next to my cursor in this app?" without guesswork.
    """
    problem = check_platform()
    if problem:
        print(f"[{FAIL}] {problem}")
        return 1

    from whisperlocal.app import (
        caret_screen_rect,
        frontmost_app,
        mouse_screen_point,
        primary_screen_height,
    )

    trusted, message = _check_accessibility()
    if not trusted:
        print(f"[{FAIL}] {message}")
        print()
        print("   Cursor detection needs Accessibility. If you are running this")
        print("   from a terminal, the terminal is what needs the permission.")
        return 1

    if args.delay:
        print(f"Switch to the app you want to test — probing in {args.delay}s...")
        time.sleep(args.delay)

    name, bundle_id = frontmost_app()
    print(f"Frontmost app : {name or '?'} ({bundle_id or '?'})")

    rect = caret_screen_rect()
    if rect:
        x, y, w, h = rect
        kind = "text cursor" if h and w == 0 else "focused element"
        print(f"Reported      : {kind}")
        print(f"  Accessibility coords  x={x:.0f} y={y:.0f} w={w:.0f} h={h:.0f}")
        print(f"  (screen origin is top-left; primary height {primary_screen_height():.0f})")
        print()
        print(f"[{OK}] The dot will appear next to your cursor in this app.")
    else:
        print("Reported      : nothing")
        point = mouse_screen_point()
        where = f"x={point[0]:.0f} y={point[1]:.0f}" if point else "unavailable"
        print(f"  Mouse pointer fallback  {where}")
        print()
        print(f"[{WARN}] This app does not report its cursor position to macOS.")
        print("       The dot will follow your mouse pointer here instead.")
        print("       Common with Electron apps and some browser text fields;")
        print("       there is nothing WhisperLocal can do about it.")
    return 0


# ─── run ─────────────────────────────────────────────────────────────────────────


def cmd_run(args: argparse.Namespace) -> int:
    """Start the menu bar app."""
    problem = check_platform()
    if problem:
        print(f"[{FAIL}] {problem}")
        return 1

    if not shutil.which("ffmpeg"):
        print(f"[{FAIL}] ffmpeg is required to decode audio but was not found.")
        print("   Install it with: brew install ffmpeg")
        print("   Then check everything with: whisperlocal doctor")
        return 1

    settings = cfg.load()
    overrides: dict[str, object] = {}
    if args.model:
        overrides["model"] = args.model
    if args.language:
        overrides["language"] = args.language
    if args.trigger:
        overrides["trigger_keys"] = tuple(
            k.strip() for k in args.trigger.split(",") if k.strip()
        )
    if overrides:
        settings = _apply_overrides(settings, overrides)

    # Imported here, not at module scope, so `doctor`, `config` and `stats`
    # still work on a machine where the audio stack is broken.
    from whisperlocal.app import run

    try:
        return run(settings)
    except KeyboardInterrupt:
        print("\nStopped")
        return 0


def _apply_overrides(settings: cfg.Settings, overrides: dict) -> cfg.Settings:
    """Apply one-off command line overrides, validating before we act on them."""
    import dataclasses

    candidate = dataclasses.replace(settings, **overrides)
    try:
        cfg.validate(candidate)
    except cfg.ConfigError as exc:
        print(f"[{FAIL}] {exc}")
        raise SystemExit(2) from None
    return candidate


# ─── argument parsing ────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whisperlocal",
        description=(
            "Push-to-talk dictation for macOS. Hold a key, speak, and your "
            "words appear at the cursor — transcribed locally, never uploaded."
        ),
        epilog="Docs: https://github.com/shivamdixit17/whisperlocal",
    )
    parser.add_argument("--version", action="version", version=f"whisperlocal {__version__}")
    parser.add_argument("--model", help="use this model for this run only")
    parser.add_argument("--language", help="spoken language code for this run only, or 'auto'")
    parser.add_argument(
        "--trigger",
        metavar="KEYS",
        help=(
            "comma-separated trigger keys for this run only, e.g. 'fn,f13'. "
            f"Supported: {', '.join(cfg.TRIGGER_KEYS)}"
        ),
    )

    sub = parser.add_subparsers(dest="command")

    doctor = sub.add_parser("doctor", help="check that everything is set up correctly")
    doctor.set_defaults(func=cmd_doctor)

    config = sub.add_parser("config", help="create, locate or print your settings")
    config.add_argument("--init", action="store_true", help="write a starter config file")
    config.add_argument("--force", action="store_true", help="overwrite an existing config")
    config.add_argument("--path", action="store_true", help="print the config file path")
    config.add_argument("--show", action="store_true", help="print the effective settings")
    config.set_defaults(func=cmd_config)

    stats = sub.add_parser("stats", help="summarise your dictation history")
    stats.add_argument("--days", type=int, help="only the last N days")
    stats.add_argument("--text", action="store_true", help="dump the transcripts too")
    stats.add_argument("--file", help="read a different history file")
    stats.add_argument("--json", action="store_true", help="print the dashboard data as JSON")
    stats.add_argument("--meetings", action="store_true", help="meeting statistics as JSON")
    stats.set_defaults(func=cmd_stats)

    dashboard = sub.add_parser("dashboard", help="open the analytics dashboard and settings page")
    dashboard.add_argument("--tab", choices=["dashboard", "settings", "meetings"])
    dashboard.add_argument("--no-browser", action="store_true", help="print the URL only")
    dashboard.set_defaults(func=cmd_dashboard)

    api_key = sub.add_parser("api-key", help="manage the cloud transcription API key")
    api_key.add_argument("action", nargs="?", choices=["status", "set", "clear"], default="status")
    api_key.set_defaults(func=cmd_api_key)

    meetings = sub.add_parser("meetings", help="list, show, search and export recorded meetings")
    meetings.add_argument(
        "action", nargs="?",
        choices=["list", "show", "search", "export", "delete", "transcribe"], default="list",
    )
    meetings.add_argument("id", nargs="?", help="meeting id (from 'meetings list')")
    meetings.add_argument("--query", help="text to search for (with 'search')")
    meetings.add_argument("--format", choices=["md", "txt", "json"], default="md")
    meetings.add_argument("--output", help="file to write (with 'export')")
    meetings.add_argument("--days", type=int, help="only the last N days")
    meetings.add_argument("--limit", type=int, default=50)
    meetings.add_argument("--backend", choices=["local", "api"], help="for 'transcribe'")
    meetings.set_defaults(func=cmd_meetings)

    install_app = sub.add_parser(
        "install-app", help="install the menu bar app so it starts at login"
    )
    install_app.add_argument(
        "--force", action="store_true", help="rebuild even if unchanged (re-signs; "
        "macOS will forget the granted permissions)"
    )
    install_app.add_argument(
        "--no-start", action="store_true", help="install but do not launch it now"
    )
    install_app.set_defaults(func=cmd_install_app)

    uninstall_app = sub.add_parser(
        "uninstall-app", help="remove the menu bar app and its login item"
    )
    uninstall_app.set_defaults(func=cmd_uninstall_app)

    probe = sub.add_parser(
        "probe-caret", help="check whether an app reports its text cursor position"
    )
    probe.add_argument(
        "--delay", type=float, default=0,
        help="seconds to wait first, so you can switch to the app to test",
    )
    probe.set_defaults(func=cmd_probe_caret)

    parser.set_defaults(func=cmd_run, command=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
