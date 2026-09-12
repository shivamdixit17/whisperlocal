"""
WhisperLocal — changing settings while the app is running.

The Settings page posts changes here. Each one is coerced and validated exactly
as config.load() would, written to config.toml, swapped into the live Settings
and announced to whoever subscribed, along with how disruptive it is:

    LIVE       read on the next use; nothing to do
    LISTENERS  the trigger listeners must be rebuilt
    RESTART    only takes effect on the next launch

Pure Python: no AppKit, no pynput. Usable from tests on any platform.
"""

from __future__ import annotations

import dataclasses
import sys
import threading
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, fields
from enum import Enum
from pathlib import Path

from whisperlocal import config as cfg
from whisperlocal import config_writer
from whisperlocal.config import ConfigError, Settings


class Tier(str, Enum):
    LIVE = "live"
    LISTENERS = "listeners"
    RESTART = "restart"


# Every Settings field, deliberately spelled out: a new field fails
# tests/test_settings_manager.py until someone decides what changing it costs.
TIER_OF: dict[str, Tier] = {
    # Trigger
    "trigger_keys": Tier.LISTENERS,
    "hold_threshold": Tier.LIVE,
    "min_recording_duration": Tier.LIVE,
    "mouse_hold_threshold": Tier.LIVE,
    "mouse_drag_cancel_px": Tier.LIVE,
    # Transcription
    "model": Tier.LIVE,
    "language": Tier.LIVE,
    "fp16": Tier.LIVE,
    # Memory
    "mlx_cache_mb": Tier.LIVE,
    "idle_release_seconds": Tier.LIVE,
    # Audio
    "sample_rate": Tier.LIVE,
    "channels": Tier.LIVE,
    # Behaviour
    "paste_mode": Tier.LIVE,
    "sounds": Tier.LIVE,
    "overlay": Tier.LIVE,
    "overlay_anchor": Tier.LIVE,
    "overlay_offset_x": Tier.LIVE,
    "overlay_offset_y": Tier.LIVE,
    # Hallucination guard
    "max_word_run": Tier.LIVE,
    "max_repeat_ratio": Tier.LIVE,
    "repeat_min_words": Tier.LIVE,
    # Backend
    "dictation_backend": Tier.LIVE,
    "meeting_backend": Tier.LIVE,
    "api_base_url": Tier.LIVE,
    "api_model": Tier.LIVE,
    "api_timeout_seconds": Tier.LIVE,
    # Meetings
    "meeting_enabled": Tier.LIVE,
    "meeting_auto_record": Tier.LIVE,
    "meeting_prompt": Tier.LIVE,
    "meeting_apps": Tier.LIVE,
    "meeting_system_audio": Tier.LIVE,
    "meeting_system_device": Tier.LIVE,
    "meeting_tap_scope": Tier.LIVE,
    "meeting_transcribe_live": Tier.LIVE,
    "meeting_model": Tier.LIVE,
    "meeting_segment_seconds": Tier.LIVE,
    "meeting_silence_db": Tier.LIVE,
    "meeting_end_grace_seconds": Tier.LIVE,
    "meeting_min_seconds": Tier.LIVE,
    "meeting_keep_audio": Tier.LIVE,
    "meeting_audio_format": Tier.LIVE,
    "meeting_transcript_words": Tier.LIVE,
    "meeting_dir": Tier.LIVE,
    # Dashboard
    "web_enabled": Tier.RESTART,
    "web_port": Tier.RESTART,
    # History
    "history_enabled": Tier.LIVE,
    "history_text": Tier.LIVE,
    "history_file": Tier.LIVE,
    # Menu bar
    "icon_idle": Tier.LIVE,
    "icon_waiting": Tier.LIVE,
    "icon_recording": Tier.LIVE,
    "icon_transcribing": Tier.LIVE,
    "icon_disabled": Tier.LIVE,
    "icon_meeting": Tier.LIVE,
    "icon_meeting_detected": Tier.LIVE,
    "icon_point_size": Tier.LIVE,
    "icon_color": Tier.LIVE,
}

_FIELD_NAMES: tuple[str, ...] = tuple(f.name for f in fields(Settings))
_DEFAULTS = Settings()

# Fields declared as tuples of numbers. config._coerce judges that by the
# first element of the default, which an empty default (icon_color) cannot
# supply, so the annotation is consulted here as well.
_FLOAT_TUPLE_FIELDS: frozenset[str] = frozenset(
    f.name for f in fields(Settings) if str(f.type).replace(" ", "") == "tuple[float,...]"
)


class EnvOverrideError(ConfigError):
    """The field is pinned by a WHISPERLOCAL_* environment variable, which
    wins over the file, so a saved value would silently do nothing."""


Subscriber = Callable[[Settings, Settings, dict[Tier, list[str]]], None]


def coerce(name: str, raw: object) -> object:
    """config._coerce, plus element typing for numeric tuples."""
    value = cfg._coerce(name, raw, getattr(_DEFAULTS, name))
    if name in _FLOAT_TUPLE_FIELDS:
        try:
            value = tuple(float(v) for v in value)  # type: ignore[union-attr]
        except (TypeError, ValueError):
            raise ConfigError(f"{name}: expected numbers, got {raw!r}") from None
    return value


def field_defaults() -> dict[str, object]:
    """{field name: default value} for every setting."""
    return {name: getattr(_DEFAULTS, name) for name in _FIELD_NAMES}


def to_jsonable(settings: Settings) -> dict[str, object]:
    """The settings as plain JSON types (tuples become lists)."""
    out: dict[str, object] = {}
    for name in _FIELD_NAMES:
        value = getattr(settings, name)
        out[name] = list(value) if isinstance(value, tuple) else value
    return out


def diff(old: Settings, new: Settings) -> list[str]:
    """Names of the fields that differ, in declaration order."""
    return [n for n in _FIELD_NAMES if getattr(old, n) != getattr(new, n)]


def classify_changes(old: Settings, new: Settings) -> dict[Tier, list[str]]:
    """The differing fields, grouped by tier. Every tier is present."""
    out: dict[Tier, list[str]] = {tier: [] for tier in Tier}
    for name in diff(old, new):
        out[TIER_OF[name]].append(name)
    return out


@dataclass
class ApplyResult:
    settings: Settings
    changed: list[str]
    applied: dict[str, list[str]]
    restart_required: bool
    warnings: list[str]
    persisted: bool


def _normalize_legacy_icons(settings: Settings) -> Settings:
    """Treat the pre-1.3 icon defaults as unset, the way config.load() does,
    so what is held in memory matches what the file will mean on next launch."""
    values = {name: getattr(settings, name) for name in _FIELD_NAMES}
    legacy = cfg.legacy_icon_keys(values)
    if not legacy:
        return settings
    return dataclasses.replace(
        settings, **{name: getattr(_DEFAULTS, name) for name in legacy}
    )


class SettingsManager:
    """Owns the live Settings and the only path that changes them."""

    def __init__(
        self,
        initial: Settings,
        *,
        config_path: Path | None = None,
        sources: dict[str, str] | None = None,
    ):
        self._current = initial
        self._config_path = config_path
        self._sources = sources
        self._subscribers: list[Subscriber] = []
        self._lock = threading.Lock()

    # ── Lazy collaborators, so tests can inject them ─────────────────────────

    @property
    def config_path(self) -> Path:
        if self._config_path is None:
            self._config_path = cfg.config_path()
        return self._config_path

    @property
    def sources(self) -> dict[str, str]:
        if self._sources is None:
            self._sources = cfg.sources()
        return self._sources

    @property
    def current(self) -> Settings:
        return self._current

    def subscribe(self, fn: Subscriber) -> None:
        self._subscribers.append(fn)

    # ── Changing things ──────────────────────────────────────────────────────

    def preview(
        self, changes: dict[str, object]
    ) -> tuple[Settings, dict[Tier, list[str]]]:
        """Coerce and validate `changes` against the current settings without
        applying them. Returns the would-be Settings and what changing to them
        would cost."""
        current = self._current
        typed: dict[str, object] = {}
        for name, raw in changes.items():
            if name not in TIER_OF or name not in _FIELD_NAMES:
                raise ConfigError(f"unknown setting {name!r}")
            value = coerce(name, raw)
            if self.sources.get(name) == "env" and value != getattr(current, name):
                raise EnvOverrideError(
                    f"{name}: set by the environment variable "
                    f"{cfg.ENV_PREFIX}{name.upper()}, which overrides the config "
                    "file. Unset it to change this setting here."
                )
            typed[name] = value

        new = _normalize_legacy_icons(dataclasses.replace(current, **typed))
        cfg.validate(new)
        return new, classify_changes(current, new)

    def apply(self, changes: dict[str, object], *, persist: bool = True) -> ApplyResult:
        """Validate, write to config.toml, swap in and notify."""
        with self._lock:
            old = self._current
            new, tiers = self.preview(changes)
            changed = diff(old, new)

            persisted = False
            if persist:
                to_write, to_remove = self._plan_write(new, changed)
                if to_write or to_remove:
                    config_writer.update_config(
                        self.config_path, to_write, remove=to_remove
                    )
                    persisted = True

            self._current = new
            for fn in list(self._subscribers):
                try:
                    fn(old, new, tiers)
                except Exception as exc:  # noqa: BLE001 - one bad listener must not block the rest
                    print(
                        f"Warning: settings subscriber {fn!r} failed: {exc}",
                        file=sys.stderr,
                    )

        return ApplyResult(
            settings=new,
            changed=changed,
            applied={tier.value: names for tier, names in tiers.items()},
            restart_required=bool(tiers[Tier.RESTART]),
            warnings=cfg.trigger_warnings(new),
            persisted=persisted,
        )

    # ── Deciding what goes in the file ───────────────────────────────────────

    def _file_values(self) -> dict[str, object]:
        try:
            with self.config_path.open("rb") as handle:
                raw = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            return {}
        if "trigger_key" in raw and "trigger_keys" not in raw:
            raw["trigger_keys"] = raw.pop("trigger_key")
        return raw

    def _plan_write(
        self, new: Settings, changed: list[str]
    ) -> tuple[dict[str, object], list[str]]:
        in_file = self._file_values()

        to_write: dict[str, object] = {}
        for name in changed:
            value = getattr(new, name)
            # A default is only worth writing when the user already has a line
            # for it — then it must be updated, not left contradicting the app.
            if name in in_file or value != getattr(_DEFAULTS, name):
                to_write[name] = value

        # Pre-1.3 template defaults the user never chose: drop them on the
        # first save so the startup note stops appearing.
        to_remove = [
            name for name in cfg.legacy_icon_keys(in_file) if name not in to_write
        ]
        return to_write, to_remove
