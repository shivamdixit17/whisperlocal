"""
WhisperLocal — the vocabulary of trigger keys.

Translates between the three ways a trigger is named: the token in
config.toml ("alt_r"), the label a person reads ("Right Option (⌥)"), and the
object a pynput listener hands us. Nothing here imports pynput; the key objects
are looked at only through their `.name`, so this runs on Linux CI and the
listener code stays thin.
"""

from __future__ import annotations

from dataclasses import dataclass

from whisperlocal import config as cfg

KIND_FN = "fn"
KIND_KEY = "key"
KIND_MOUSE = "mouse"


@dataclass(frozen=True)
class TriggerInfo:
    token: str
    label: str
    risky: bool
    kind: str  # KIND_FN | KIND_KEY | KIND_MOUSE


def _kind(token: str) -> str:
    if token == cfg.FN_KEY:
        return KIND_FN
    if token in cfg.MOUSE_BUTTONS:
        return KIND_MOUSE
    return KIND_KEY


def label_for(token: str) -> str:
    """What Settings.trigger_label calls this key."""
    return cfg.KEY_LABELS.get(token, token.upper())


def catalog() -> list[TriggerInfo]:
    """Every supported trigger, in the order config.TRIGGER_KEYS lists them."""
    return [
        TriggerInfo(
            token=token,
            label=label_for(token),
            risky=token in cfg.RISKY_KEYS,
            kind=_kind(token),
        )
        for token in cfg.TRIGGER_KEYS
    ]


# pynput Key.name -> trigger token. Every keyboard token names itself; the
# aliases cover pynput's platform quirks. On macOS Key.alt_l *is* Key.alt (one
# object, named "alt") and Key.alt_gr is Key.alt_r. The left Command, Control
# and Shift are deliberately absent: they are not supported triggers.
_ALIASES: dict[str, str] = {
    "alt": "alt_l",
    "alt_gr": "alt_r",
}
_NAME_TO_TOKEN: dict[str, str] = {
    token: token for token in cfg.TRIGGER_KEYS if _kind(token) == KIND_KEY
} | _ALIASES

_BUTTON_TO_TOKEN: dict[str, str] = {
    "left": cfg.MOUSE_LEFT,
    "right": cfg.MOUSE_RIGHT,
    "middle": cfg.MOUSE_MIDDLE,
}


def token_for_key(key: object) -> str | None:
    """The trigger token for a pynput Key member, or None if it is not a
    supported trigger (including any KeyCode, which has no name)."""
    name = getattr(key, "name", None)
    if not isinstance(name, str):
        return None
    return _NAME_TO_TOKEN.get(name)


def key_display_name(key: object) -> str:
    """Something to show in an 'unsupported key' message."""
    name = getattr(key, "name", None)
    if isinstance(name, str) and name:
        return name
    char = getattr(key, "char", None)
    if isinstance(char, str) and char:
        return char
    return repr(key)


def token_for_button(name: str) -> str | None:
    """The trigger token for a pynput mouse Button name."""
    return _BUTTON_TO_TOKEN.get(name)
