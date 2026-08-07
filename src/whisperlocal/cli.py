"""
WhisperLocal — command line entry point.

    whisperlocal            start the menu bar app
    whisperlocal doctor     check that everything is set up correctly
    whisperlocal config     create, locate or print your settings
    whisperlocal stats      summarise your dictation history
"""

from __future__ import annotations

import argparse
import platform
import shutil
import sys
from pathlib import Path

from whisperlocal import __version__
from whisperlocal import config as cfg

# System Settings panes we point people at, as deep links macOS understands.
PANE = "x-apple.systempreferences:com.apple.preference.security"
PERMISSION_PANES = {
    "Accessibility": f"{PANE}?Privacy_Accessibility",
    "Microphone": f"{PANE}?Privacy_Microphone",
    "Input Monitoring": f"{PANE}?Privacy_ListenEvent",
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
    for ok, message in (_check_accessibility(), _check_model_cached(settings)):
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

    print()
    print("These permissions belong to the app that launches WhisperLocal")
    print("(your terminal, or whatever wrapper you use). Grant all three:")
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
    return stats.report(settings, days=args.days, show_text=args.text, path=path)


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
    stats.set_defaults(func=cmd_stats)

    parser.set_defaults(func=cmd_run, command=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
