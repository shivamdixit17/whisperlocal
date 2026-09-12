"""keymap translates pynput key objects and config tokens without pynput."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from whisperlocal import config as cfg
from whisperlocal import keymap


def test_does_not_import_pynput():
    # Nothing in the test suite imports pynput, so if it is loaded, keymap did it.
    assert "whisperlocal.keymap" in sys.modules
    assert "pynput" not in sys.modules


class FakeKey:
    """Stands in for a pynput Key member: has a .name, no .char."""

    def __init__(self, name: str):
        self.name = name

    def __repr__(self):
        return f"Key.{self.name}"


class FakeKeyCode:
    """Stands in for a pynput KeyCode: has .char and .vk, no .name."""

    def __init__(self, char: str | None, vk: int = 0):
        self.char = char
        self.vk = vk

    def __repr__(self):
        return f"<{self.vk}>" if self.char is None else repr(self.char)


# ─── token_for_key ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("alt", "alt_l"),       # macOS: Key.alt_l is Key.alt
        ("alt_l", "alt_l"),     # other platforms
        ("alt_r", "alt_r"),
        ("alt_gr", "alt_r"),
        ("cmd_r", "cmd_r"),
        ("ctrl_r", "ctrl_r"),
        ("shift_r", "shift_r"),
        ("f13", "f13"),
        ("f19", "f19"),
        ("f12", None),
        ("space", None),
        ("cmd", None),
        ("cmd_l", None),
        ("ctrl", None),
        ("ctrl_l", None),
        ("shift", None),
        ("shift_l", None),
        ("fn", None),           # Fn never arrives through pynput
        ("mouse_left", None),   # nor do mouse buttons
    ],
)
def test_token_for_key(name, expected):
    assert keymap.token_for_key(FakeKey(name)) == expected


def test_every_keyboard_token_maps_to_itself():
    for token in cfg.TRIGGER_KEYS:
        if token == cfg.FN_KEY or token in cfg.MOUSE_BUTTONS:
            continue
        assert keymap.token_for_key(FakeKey(token)) == token


def test_token_for_key_without_name():
    assert keymap.token_for_key(FakeKeyCode("a")) is None
    assert keymap.token_for_key(FakeKeyCode(None, vk=179)) is None
    assert keymap.token_for_key(object()) is None
    assert keymap.token_for_key(None) is None
    assert keymap.token_for_key(SimpleNamespace(name=42)) is None


# ─── key_display_name ────────────────────────────────────────────────────────────


def test_key_display_name():
    assert keymap.key_display_name(FakeKey("space")) == "space"
    assert keymap.key_display_name(FakeKeyCode("a")) == "a"
    assert keymap.key_display_name(FakeKeyCode(None, vk=179)) == "<179>"
    assert keymap.key_display_name(SimpleNamespace(name="", char="")) == "namespace(name='', char='')"


# ─── token_for_button ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("left", cfg.MOUSE_LEFT),
        ("right", cfg.MOUSE_RIGHT),
        ("middle", cfg.MOUSE_MIDDLE),
        ("button8", None),
        ("unknown", None),
        ("", None),
    ],
)
def test_token_for_button(name, expected):
    assert keymap.token_for_button(name) == expected


# ─── catalog / label_for ─────────────────────────────────────────────────────────


def test_catalog_order_and_contents():
    entries = keymap.catalog()
    assert [e.token for e in entries] == list(cfg.TRIGGER_KEYS)
    by_token = {e.token: e for e in entries}

    for token in cfg.TRIGGER_KEYS:
        entry = by_token[token]
        assert entry.label == cfg.KEY_LABELS.get(token, token.upper())
        assert entry.label == cfg.Settings(trigger_keys=(token,)).trigger_label
        assert entry.risky == (token in cfg.RISKY_KEYS)
        if token == cfg.FN_KEY:
            assert entry.kind == "fn"
        elif token in cfg.MOUSE_BUTTONS:
            assert entry.kind == "mouse"
        else:
            assert entry.kind == "key"

    assert by_token["fn"].label == "Fn (globe)"
    assert by_token["f13"].label == "F13"
    assert by_token["alt_r"].risky is False
    assert by_token["alt_l"].risky is True
    assert by_token["cmd_r"].risky is True
    assert by_token["mouse_left"].risky is True


def test_trigger_info_is_frozen():
    entry = keymap.catalog()[0]
    with pytest.raises(Exception):
        entry.label = "x"  # type: ignore[misc]


def test_label_for():
    assert keymap.label_for("fn") == "Fn (globe)"
    assert keymap.label_for("mouse_middle") == "middle mouse button"
    assert keymap.label_for("f17") == "F17"
