"""
WhisperLocal — configuration.

Settings resolve in four layers, each overriding the one before it:

    1. the defaults in this file
    2. ~/.config/whisperlocal/config.toml
    3. WHISPERLOCAL_* environment variables
    4. command line flags

Nothing is required. With no config file at all the defaults give you a
working push-to-talk setup on the Fn key.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

# ─── Trigger keys ────────────────────────────────────────────────────────────────
# Names accepted in `trigger_keys`. You can list several; holding any one of
# them dictates.
#
#   "fn"  is the globe/Fn key. It is watched through a Quartz event tap rather
#         than pynput, because Fn is not a keycode at all — it only appears as a
#         modifier flag bit — and pynput's key enum has no entry for it.
#
# Everything else is a pynput Key name. Only keys that are safe to hold are
# offered: right-hand modifiers you rarely press alone, and the F13-F19 block
# most keyboards never send.
FN_KEY = "fn"

# Mouse buttons are watched by a pynput mouse listener rather than the keyboard
# one. They get their own hold threshold and a drag guard — see MOUSE_BUTTONS.
MOUSE_LEFT = "mouse_left"
MOUSE_RIGHT = "mouse_right"
MOUSE_MIDDLE = "mouse_middle"
MOUSE_BUTTONS: frozenset[str] = frozenset({MOUSE_LEFT, MOUSE_RIGHT, MOUSE_MIDDLE})

TRIGGER_KEYS: tuple[str, ...] = (
    FN_KEY,
    "alt_r",
    "alt_l",
    "cmd_r",
    "ctrl_r",
    "shift_r",
    "f13",
    "f14",
    "f15",
    "f16",
    "f17",
    "f18",
    "f19",
    MOUSE_LEFT,
    MOUSE_RIGHT,
    MOUSE_MIDDLE,
)

KEY_LABELS: dict[str, str] = {
    FN_KEY: "Fn (globe)",
    "alt_r": "Right Option (⌥)",
    "alt_l": "Left Option (⌥)",
    "cmd_r": "Right Command (⌘)",
    "ctrl_r": "Right Control (⌃)",
    "shift_r": "Right Shift (⇧)",
    MOUSE_LEFT: "left mouse button",
    MOUSE_RIGHT: "right mouse button",
    MOUSE_MIDDLE: "middle mouse button",
}

# Holding one of these to dictate also holds down something the rest of macOS
# is already using. Allowed, but warned about at startup.
#
# The left button is the worst offender: every drag, text selection, window
# move and scrollbar grab holds it down. mouse_hold_threshold and
# mouse_drag_cancel_px exist to make it usable at all — see Settings.
RISKY_KEYS: frozenset[str] = frozenset({"cmd_r", "ctrl_r", "alt_l"}) | MOUSE_BUTTONS

# ─── Models ──────────────────────────────────────────────────────────────────────
# Short names for the mlx-community Whisper builds. Any Hugging Face repo id
# also works if you set `model` to the full "org/name" string.
#
# Note the "-mlx" suffix: the un-suffixed repos (mlx-community/whisper-base,
# whisper-small) do not resolve and return HTTP 401 on download.
MODEL_ALIASES: dict[str, str] = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}

# ─── UI ──────────────────────────────────────────────────────────────────────────
APP_NAME = "WhisperLocal"


@dataclass(frozen=True)
class Settings:
    """The resolved configuration for one run."""

    # ── Trigger ──────────────────────────────────────────────────────────────
    # Hold any one of these to dictate. Listing several is fine: the first one
    # pressed owns the recording until it is released, so pressing a second
    # trigger mid-sentence changes nothing.
    trigger_keys: tuple[str, ...] = (FN_KEY,)

    # Seconds to hold before recording starts. 0 begins the instant the key goes
    # down, so you can talk straight away instead of waiting out an arming
    # delay; stray taps are still thrown away afterwards by
    # min_recording_duration, so nothing gets pasted from a brush of the key.
    hold_threshold: float = 0.0
    min_recording_duration: float = 0.5

    # Mouse buttons get their own, much longer threshold. hold_threshold is
    # often 0, which is fine for a key you never press otherwise but would make
    # every single click start a recording.
    mouse_hold_threshold: float = 1.0

    # A press that travels further than this many pixels is a drag — selecting
    # text, moving a window, working a slider — not someone holding still to
    # dictate. Passing it cancels the trigger and discards whatever was
    # captured. This is what makes the left button usable as a trigger at all.
    # Set to 0 to disable the guard.
    mouse_drag_cancel_px: int = 10

    # ── Transcription ────────────────────────────────────────────────────────
    model: str = "base"
    language: str = "en"
    fp16: bool = False

    # ── Audio ────────────────────────────────────────────────────────────────
    # Fallback only. Recording actually runs at the input device's native rate,
    # because asking PortAudio for a rate the hardware does not run at pushes it
    # through a rate-adapting path that segfaults on CoreAudio's realtime
    # thread. Whisper's ffmpeg loader downsamples when it reads the file.
    sample_rate: int = 16000
    channels: int = 1

    # ── Behaviour ────────────────────────────────────────────────────────────
    paste_mode: str = "paste"  # "paste" presses ⌘V for you; "clipboard" copies only
    sounds: bool = True
    overlay: bool = True

    # ── Hallucination guard ──────────────────────────────────────────────────
    # Whisper falls into a decoder repetition loop on short or noisy audio and
    # emits one word over and over. Observed: "ARP" x112, "funny" x223 and
    # "On 25 25 25 25...". Output matching either test is discarded, not pasted.
    #
    # max_word_run catches short loops; the ratio catches long ones. Tuned so
    # real speech survives: "Hello, hello, hello." is a run of 3, and "no no no
    # I really do not think that is right" scores 0.85. Measured hallucinations
    # score 0.01-0.29.
    max_word_run: int = 4
    max_repeat_ratio: float = 0.30
    repeat_min_words: int = 5

    # ── Dictation history ────────────────────────────────────────────────────
    # Every dictation is appended to a JSONL file, one object per line:
    # append-only, so a crash costs at most a partial trailing line, and it
    # reads straight into pandas.read_json(..., lines=True) or jq.
    #
    # Deliberately NOT in ~/Documents, which is iCloud-synced on many Macs and
    # would upload a complete record of everything you dictate. Application
    # Support is not synced.
    #
    # With history_text on, this is a permanent, unencrypted, plain-text record
    # of everything you say. Set history_text = false to keep the statistics but
    # not the words. Deleting the file is a complete purge; nothing is indexed
    # anywhere else.
    history_enabled: bool = True
    history_text: bool = True
    history_file: str = "~/Library/Application Support/WhisperLocal/history.jsonl"

    # ── Menu bar icons ───────────────────────────────────────────────────────
    # SF Symbol names (macOS 11+). Any name from Apple's SF Symbols app works.
    icon_idle: str = "mic"
    icon_waiting: str = "hourglass"
    icon_recording: str = "mic.fill"
    icon_transcribing: str = "waveform"
    icon_disabled: str = "mic.slash"
    icon_point_size: int = 15

    # Menu bar glyph colour as (r, g, b) in 0-1, or empty for a template image.
    # A template renders monochrome and follows the menu bar automatically in
    # light and dark mode; a fixed colour does not, so this orange is one that
    # reads on both.
    icon_color: tuple[float, ...] = (1.00, 0.58, 0.00)

    # ── Derived ──────────────────────────────────────────────────────────────

    @property
    def model_path(self) -> str:
        """Full Hugging Face repo id for the selected model."""
        return MODEL_ALIASES.get(self.model, self.model)

    @property
    def whisper_language(self) -> str | None:
        """Language passed to Whisper. "auto" means detect it."""
        return None if self.language == "auto" else self.language

    @property
    def uses_fn(self) -> bool:
        """Whether the Fn event tap is needed."""
        return FN_KEY in self.trigger_keys

    @property
    def pynput_keys(self) -> tuple[str, ...]:
        """Trigger keys that the ordinary pynput keyboard listener handles."""
        return tuple(
            k for k in self.trigger_keys if k != FN_KEY and k not in MOUSE_BUTTONS
        )

    @property
    def mouse_buttons(self) -> tuple[str, ...]:
        """Trigger buttons that the pynput mouse listener handles."""
        return tuple(k for k in self.trigger_keys if k in MOUSE_BUTTONS)

    def threshold_for(self, token: object) -> float:
        """Hold time required before this particular trigger starts recording."""
        if isinstance(token, str) and token in MOUSE_BUTTONS:
            return max(0.0, self.mouse_hold_threshold)
        return max(0.0, self.hold_threshold)

    @property
    def trigger_label(self) -> str:
        """Human-readable trigger description, e.g. 'Fn (globe) or F13'."""
        names = [KEY_LABELS.get(k, k.upper()) for k in self.trigger_keys]
        if len(names) == 1:
            return names[0]
        return ", ".join(names[:-1]) + f" or {names[-1]}"

    @property
    def history_path(self) -> Path:
        return Path(self.history_file).expanduser()

    @property
    def icon_rgb(self) -> tuple[float, float, float] | None:
        """Icon colour as a 3-tuple, or None for a template image."""
        if not self.icon_color or len(self.icon_color) != 3:
            return None
        return (self.icon_color[0], self.icon_color[1], self.icon_color[2])


# ─── Paths ───────────────────────────────────────────────────────────────────────


def config_dir() -> Path:
    """Directory holding config.toml. Honors XDG_CONFIG_HOME."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "whisperlocal"


def config_path() -> Path:
    return config_dir() / "config.toml"


def cache_dir() -> Path:
    """Scratch space for the in-flight recording. Safe to delete any time."""
    return Path.home() / "Library" / "Caches" / "WhisperLocal"


def temp_audio_file() -> Path:
    return cache_dir() / "recording.wav"


# ─── Loading ─────────────────────────────────────────────────────────────────────

ENV_PREFIX = "WHISPERLOCAL_"


class ConfigError(ValueError):
    """A setting was present but not usable."""


def _as_bool(name: str, raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name}: expected true or false, got {raw!r}")


def _as_seq(name: str, raw: object) -> tuple:
    """Accept a TOML array, or a comma-separated string from an env var."""
    if isinstance(raw, (list, tuple)):
        return tuple(raw)
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if not parts:
            raise ConfigError(f"{name}: is empty")
        return tuple(parts)
    raise ConfigError(f"{name}: expected a list, got {raw!r}")


def _coerce(name: str, raw: object, default: object) -> object:
    """Turn a TOML or environment value into the type the field expects."""
    if isinstance(default, bool):
        return _as_bool(name, raw)

    if isinstance(default, tuple):
        seq = _as_seq(name, raw)
        # icon_color is numeric; trigger_keys is strings.
        if default and isinstance(default[0], float):
            try:
                return tuple(float(v) for v in seq)
            except (TypeError, ValueError):
                raise ConfigError(f"{name}: expected numbers, got {raw!r}") from None
        return tuple(str(v) for v in seq)

    if isinstance(default, float):
        try:
            return float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ConfigError(f"{name}: expected a number, got {raw!r}") from None

    if isinstance(default, int):
        try:
            return int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ConfigError(f"{name}: expected a whole number, got {raw!r}") from None

    return str(raw)


def validate(settings: Settings) -> None:
    """Reject values that would otherwise fail later in a confusing way."""
    if not settings.trigger_keys:
        raise ConfigError(
            "trigger_keys: at least one key is required. "
            f"Choose from: {', '.join(TRIGGER_KEYS)}"
        )

    unknown = [k for k in settings.trigger_keys if k not in TRIGGER_KEYS]
    if unknown:
        raise ConfigError(
            f"trigger_keys: {', '.join(repr(k) for k in unknown)} not supported. "
            f"Choose from: {', '.join(TRIGGER_KEYS)}"
        )

    if len(set(settings.trigger_keys)) != len(settings.trigger_keys):
        raise ConfigError("trigger_keys: the same key is listed more than once")

    if settings.paste_mode not in ("paste", "clipboard"):
        raise ConfigError(
            f"paste_mode: {settings.paste_mode!r} is not supported. "
            "Choose 'paste' or 'clipboard'."
        )

    if "/" not in settings.model_path:
        raise ConfigError(
            f"model: {settings.model!r} is not a known name or a Hugging Face "
            f"repo id. Known names: {', '.join(MODEL_ALIASES)}"
        )

    if settings.hold_threshold < 0:
        raise ConfigError("hold_threshold: must be zero or more")
    if settings.min_recording_duration < 0:
        raise ConfigError("min_recording_duration: must be zero or more")
    if settings.mouse_hold_threshold < 0:
        raise ConfigError("mouse_hold_threshold: must be zero or more")
    if settings.mouse_drag_cancel_px < 0:
        raise ConfigError("mouse_drag_cancel_px: must be zero or more")

    # A mouse button that fires instantly would trigger on every click.
    if settings.mouse_buttons and settings.mouse_hold_threshold < 0.3:
        raise ConfigError(
            "mouse_hold_threshold: must be at least 0.3 when a mouse button is a "
            "trigger, otherwise ordinary clicking starts recordings"
        )
    if settings.sample_rate <= 0:
        raise ConfigError("sample_rate: must be greater than zero")
    if settings.max_word_run < 2:
        raise ConfigError("max_word_run: must be 2 or more")
    if not 0 < settings.max_repeat_ratio <= 1:
        raise ConfigError("max_repeat_ratio: must be between 0 and 1")

    if settings.icon_color and len(settings.icon_color) not in (0, 3):
        raise ConfigError("icon_color: expected three numbers (r, g, b) or an empty list")


def _read_toml(path: Path) -> dict[str, object]:
    """Read the config file, tolerating its absence."""
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        print(f"Warning: {path} is not valid TOML ({exc}) — using defaults", file=sys.stderr)
        return {}
    except OSError as exc:
        print(f"Warning: could not read {path} ({exc}) — using defaults", file=sys.stderr)
        return {}


def load(*, strict: bool = False) -> Settings:
    """
    Resolve settings from defaults, the config file, then the environment.

    A bad value warns and falls back to the default, so a typo never stops you
    from dictating. strict=True raises ConfigError instead, which is what
    `whisperlocal config --show` uses.
    """
    defaults = Settings()
    known = {f.name for f in fields(Settings)}
    resolved: dict[str, object] = {}

    for key, value in _read_toml(config_path()).items():
        if key == "trigger_key":
            # Accepted for compatibility with the single-key setting this
            # replaced, so an older config keeps working untouched.
            resolved["trigger_keys"] = value
            continue
        if key not in known:
            print(f"Warning: unknown setting {key!r} in config.toml — ignored", file=sys.stderr)
            continue
        resolved[key] = value

    for name in known:
        env_value = os.environ.get(ENV_PREFIX + name.upper())
        if env_value is not None:
            resolved[name] = env_value
    legacy_env = os.environ.get(ENV_PREFIX + "TRIGGER_KEY")
    if legacy_env is not None:
        resolved["trigger_keys"] = legacy_env

    typed: dict[str, object] = {}
    for name, value in resolved.items():
        try:
            typed[name] = _coerce(name, value, getattr(defaults, name))
        except ConfigError as exc:
            if strict:
                raise
            print(f"Warning: {exc} — using the default", file=sys.stderr)

    settings = Settings(**typed)  # type: ignore[arg-type]

    try:
        validate(settings)
    except ConfigError as exc:
        if strict:
            raise
        print(f"Warning: {exc} — falling back to defaults", file=sys.stderr)
        return Settings()

    return settings


# ─── Config file template ────────────────────────────────────────────────────────

TEMPLATE = f"""\
# WhisperLocal configuration
# Location: ~/.config/whisperlocal/config.toml
#
# Everything here is optional — delete anything you do not want to change.
# Any setting can also be overridden for one run with an environment variable,
# e.g.  WHISPERLOCAL_MODEL=small whisperlocal

# ─── Trigger ────────────────────────────────────────────────────────────────
# Hold any one of these to dictate. List as many as you like.
# Supported: {", ".join(TRIGGER_KEYS)}
#
# "fn" is the globe/Fn key, watched through a Quartz event tap because pynput
# cannot see it. Mixing "fn" with ordinary keys and mouse buttons is fine —
# each kind gets its own listener and they all run together.
#
# Whichever trigger you press first owns the recording until you let go, so
# pressing a second one mid-sentence does not cut you off.
#
# Example — Fn, a spare function key, and the middle mouse button:
#   trigger_keys = ["fn", "f13", "mouse_middle"]
trigger_keys = ["fn"]

# Seconds to hold before recording starts. 0 records the moment the key goes
# down, so you can start talking immediately. Accidental taps are still
# discarded by min_recording_duration below, so nothing gets pasted from them.
hold_threshold = 0.0

# Recordings shorter than this (seconds) are thrown away as noise.
min_recording_duration = 0.5

# ─── Mouse triggers ─────────────────────────────────────────────────────────
# Add "mouse_left", "mouse_right" or "mouse_middle" to trigger_keys above to
# dictate by holding a mouse button.
#
# Read this before turning it on. Holding the LEFT button is what every drag,
# text selection, window move and slider does, so it needs both guards below to
# be usable. The right or middle button is a far safer choice.

# Mouse buttons use this instead of hold_threshold, which is often 0 — fine for
# a key you never otherwise press, but it would make every click record.
# Minimum 0.3.
mouse_hold_threshold = 1.0

# A press that moves further than this many pixels is a drag, not someone
# holding still to talk: the trigger is cancelled and the audio discarded.
# This is what keeps the left button from firing while you select text.
# Set to 0 to turn the guard off.
mouse_drag_cancel_px = 10

# ─── Transcription ──────────────────────────────────────────────────────────
# A short name ({", ".join(MODEL_ALIASES)}) or any Hugging Face repo id.
# Bigger is more accurate and slower.
model = "base"

# Two-letter language code ("en", "de", "es", "hi", ...) or "auto" to detect.
# Naming the language is faster and more accurate than autodetection.
language = "en"

# Half precision. Off by default because that is the configuration this has
# been used and tuned against; turning it on is usually faster.
fp16 = false

# ─── Behaviour ──────────────────────────────────────────────────────────────
# "paste"     — copy and press Cmd+V for you (needs Accessibility permission)
# "clipboard" — only copy; you paste it yourself. Use this in apps that reject
#               synthetic keystrokes, or to avoid granting Accessibility.
paste_mode = "paste"

# System sounds on start, stop, success and failure.
sounds = true

# The small pulsing dot near the bottom of the screen while recording.
overlay = true

# ─── Dictation history ──────────────────────────────────────────────────────
# Every dictation, successful or not, is appended to a JSONL file. This is what
# `whisperlocal stats` reads. Failures are logged too — the hallucination rate
# is only measurable if they are.
#
# WARNING: with history_text on, this file is a permanent, unencrypted,
# plain-text record of everything you dictate. It lives in Application Support
# (not Documents, which iCloud syncs). Delete the file to purge it completely.
history_enabled = true

# Set false to keep the statistics — counts, timings, which app — but not store
# the transcribed words themselves.
history_text = true

history_file = "~/Library/Application Support/WhisperLocal/history.jsonl"

# ─── Hallucination guard ────────────────────────────────────────────────────
# Whisper loops on short or noisy audio, emitting one word over and over.
# Output failing either test below is discarded instead of pasted.

# Longest allowed run of the same word back to back.
max_word_run = 4

# Below this ratio of unique words to total words, the output is a loop.
max_repeat_ratio = 0.30

# The ratio test needs a few words to mean anything; shorter output is judged
# by the run test alone.
repeat_min_words = 5

# ─── Menu bar ───────────────────────────────────────────────────────────────
# SF Symbol names — any name from Apple's SF Symbols app works.
icon_idle = "mic"
icon_waiting = "hourglass"
icon_recording = "mic.fill"
icon_transcribing = "waveform"
icon_disabled = "mic.slash"
icon_point_size = 15

# Glyph colour as (r, g, b) from 0 to 1. Use an empty list [] for a template
# image, which renders monochrome and follows light/dark mode automatically.
icon_color = [1.00, 0.58, 0.00]
"""
