"""
WhisperLocal — configuration.

Settings are resolved in three layers, each overriding the one before it:

    1. the defaults in this file
    2. ~/.config/whisperlocal/config.toml
    3. WHISPERLOCAL_* environment variables

Nothing here is required. With no config file and no environment variables,
the defaults give you a working push-to-talk setup.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

# ─── Models ──────────────────────────────────────────────────────────────────────
# Whisper checkpoints converted to MLX by the mlx-community org on Hugging Face.
# Weights download on first use and are cached in ~/.cache/huggingface.
#
# Use the "-mlx" suffixed repo names. The unsuffixed ones (whisper-base,
# whisper-small) no longer resolve and fail with an HTTP 401 on download.
MODEL_OPTIONS: dict[str, str] = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}

# ─── Trigger keys ────────────────────────────────────────────────────────────────
# Names accepted by `trigger_key`. Each maps to a pynput Key attribute.
# Only keys that are safe to hold down without side effects are offered:
# modifiers you rarely use on their own, and the F13-F19 block that most
# keyboards never send.
TRIGGER_KEYS: tuple[str, ...] = (
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
)

# Pretty names for the console banner and menu bar.
KEY_LABELS: dict[str, str] = {
    "alt_r": "Right Option (⌥)",
    "alt_l": "Left Option (⌥)",
    "cmd_r": "Right Command (⌘)",
    "ctrl_r": "Right Control (⌃)",
    "shift_r": "Right Shift (⇧)",
}

# ─── UI ──────────────────────────────────────────────────────────────────────────
APP_NAME = "WhisperLocal"
ICON_IDLE = "🎙️"
ICON_WAITING = "⏳"
ICON_RECORDING = "🔴"
ICON_TRANSCRIBING = "⚙️"


@dataclass(frozen=True)
class Settings:
    """The resolved configuration for one run."""

    # Trigger
    trigger_key: str = "alt_r"
    hold_threshold: float = 1.0
    min_recording_duration: float = 0.5

    # Transcription
    model: str = "base"
    language: str = "en"
    fp16: bool = True

    # Audio
    sample_rate: int = 16000

    # Behaviour
    paste_mode: str = "paste"  # "paste" (⌘V at cursor) or "clipboard" (copy only)
    sounds: bool = True
    overlay: bool = True

    @property
    def model_path(self) -> str:
        """The Hugging Face repo for the selected model."""
        return MODEL_OPTIONS[self.model]

    @property
    def trigger_label(self) -> str:
        """A human-readable name for the trigger key."""
        return KEY_LABELS.get(self.trigger_key, self.trigger_key.upper())

    @property
    def whisper_language(self) -> str | None:
        """Language passed to Whisper. `auto` means "detect it"."""
        return None if self.language == "auto" else self.language


# ─── Paths ───────────────────────────────────────────────────────────────────────


def config_dir() -> Path:
    """Directory holding config.toml. Honors XDG_CONFIG_HOME if set."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "whisperlocal"


def config_path() -> Path:
    """Full path to the user's config file (which may not exist)."""
    return config_dir() / "config.toml"


def cache_dir() -> Path:
    """Scratch directory for recorded audio. Safe to delete at any time."""
    return Path.home() / "Library" / "Caches" / "WhisperLocal"


def temp_audio_file() -> Path:
    """Where the current recording is written before transcription."""
    return cache_dir() / "recording.wav"


# ─── Loading ─────────────────────────────────────────────────────────────────────

# Environment variable → field name. Every setting is overridable.
ENV_PREFIX = "WHISPERLOCAL_"


class ConfigError(ValueError):
    """A setting was present but not usable."""


def _coerce(name: str, raw: object, target: type) -> object:
    """Turn a TOML/env value into the type the dataclass field expects."""
    if target is bool:
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        raise ConfigError(f"{name}: expected true or false, got {raw!r}")

    if target is float:
        try:
            return float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ConfigError(f"{name}: expected a number, got {raw!r}") from None

    if target is int:
        try:
            return int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ConfigError(f"{name}: expected a whole number, got {raw!r}") from None

    return str(raw)


def validate(settings: Settings) -> None:
    """Reject values that would fail later in a confusing way."""
    if settings.model not in MODEL_OPTIONS:
        raise ConfigError(
            f"model: {settings.model!r} is not available. "
            f"Choose one of: {', '.join(MODEL_OPTIONS)}"
        )

    if settings.trigger_key not in TRIGGER_KEYS:
        raise ConfigError(
            f"trigger_key: {settings.trigger_key!r} is not supported. "
            f"Choose one of: {', '.join(TRIGGER_KEYS)}"
        )

    if settings.paste_mode not in ("paste", "clipboard"):
        raise ConfigError(
            f"paste_mode: {settings.paste_mode!r} is not supported. "
            "Choose 'paste' or 'clipboard'."
        )

    if settings.hold_threshold < 0:
        raise ConfigError("hold_threshold: must be zero or more")

    if settings.min_recording_duration < 0:
        raise ConfigError("min_recording_duration: must be zero or more")

    if settings.sample_rate <= 0:
        raise ConfigError("sample_rate: must be greater than zero")


def _read_toml(path: Path) -> dict[str, object]:
    """Read the config file, tolerating its absence."""
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        print(f"⚠️  {path} is not valid TOML ({exc}) — using defaults", file=sys.stderr)
        return {}
    except OSError as exc:
        print(f"⚠️  Could not read {path} ({exc}) — using defaults", file=sys.stderr)
        return {}


def load(*, strict: bool = False) -> Settings:
    """
    Resolve settings from defaults, then the config file, then the environment.

    A bad value warns and falls back to the default so a typo in the config
    file never stops you from dictating. Pass strict=True (used by
    `whisperlocal config --show`) to raise ConfigError instead.
    """
    defaults = Settings()
    known = {f.name for f in fields(Settings)}
    resolved: dict[str, object] = {}

    file_values = _read_toml(config_path())
    for key, value in file_values.items():
        if key not in known:
            print(f"⚠️  Unknown setting in config.toml: {key!r} — ignored", file=sys.stderr)
            continue
        resolved[key] = value

    for name in known:
        env_value = os.environ.get(ENV_PREFIX + name.upper())
        if env_value is not None:
            resolved[name] = env_value

    # Coerce each value to its declared type, one at a time, so a single bad
    # entry only costs you that one setting.
    typed: dict[str, object] = {}
    for name, value in resolved.items():
        target = type(getattr(defaults, name))
        try:
            typed[name] = _coerce(name, value, target)
        except ConfigError as exc:
            if strict:
                raise
            print(f"⚠️  {exc} — using the default", file=sys.stderr)

    settings = Settings(**typed)  # type: ignore[arg-type]

    try:
        validate(settings)
    except ConfigError as exc:
        if strict:
            raise
        print(f"⚠️  {exc} — using defaults for the rest", file=sys.stderr)
        return Settings()

    return settings


# ─── Config file template ────────────────────────────────────────────────────────

TEMPLATE = f"""\
# WhisperLocal configuration
# Location: ~/.config/whisperlocal/config.toml
#
# Every setting below is optional — delete anything you do not want to change.
# Each one can also be overridden for a single run with an environment
# variable, e.g. WHISPERLOCAL_MODEL=small whisperlocal

# ─── Trigger ────────────────────────────────────────────────────────────────
# The key you hold down to dictate. Supported values:
# {", ".join(TRIGGER_KEYS)}
trigger_key = "alt_r"

# Seconds to hold the key before recording starts. This is what stops a
# stray tap on the key from opening the microphone. Set to 0 for instant.
hold_threshold = 1.0

# Recordings shorter than this (in seconds) are discarded as noise.
min_recording_duration = 0.5

# ─── Transcription ──────────────────────────────────────────────────────────
# Which model to use. Bigger is more accurate and slower:
# {", ".join(MODEL_OPTIONS)}
model = "base"

# Spoken language as a two-letter code ("en", "de", "es", "hi", ...),
# or "auto" to let Whisper detect it. Naming the language is faster and
# more accurate than autodetection when you know it.
language = "en"

# Half precision. Faster on Apple Silicon and the recommended default.
fp16 = true

# ─── Audio ──────────────────────────────────────────────────────────────────
# Whisper is trained on 16 kHz audio. Changing this is rarely useful.
sample_rate = 16000

# ─── Behaviour ──────────────────────────────────────────────────────────────
# "paste"     — copy the text and press Cmd+V for you (needs Accessibility)
# "clipboard" — only copy it; you paste it yourself. Use this in apps that
#               reject synthetic keystrokes, or if you would rather not
#               grant Accessibility permission.
paste_mode = "paste"

# System sounds on start, stop, success and failure.
sounds = true

# The floating on-screen indicator while recording and transcribing.
overlay = true
"""
