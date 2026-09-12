"""
WhisperLocal — writing config.toml without losing the user's comments.

The config file is a document the user owns: it is full of explanatory
comments and they may have edited it by hand. Saving from the Settings page
therefore edits lines in place rather than serialising a dict. Only two kinds
of change are ever made — replace the value on a `key = ...` line, or append a
`key = ...` line under one clearly marked section at the end — and the result
is parsed back and checked before it touches the disk.

Pure Python: no AppKit, no pynput. Usable from tests on any platform.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import tomllib
from collections.abc import Iterable
from dataclasses import fields
from pathlib import Path

from whisperlocal import config as cfg

# The one place the writer adds lines the template never had. Created once,
# reused on every later save.
APPEND_HEADER = "# ─── Set from the Settings page ─────────────────────────────────────────────"

LEGACY_TRIGGER_KEY = "trigger_key"


class ConfigWriteError(OSError):
    """The edited text was fine but it could not be written to disk."""


# ─── Formatting ──────────────────────────────────────────────────────────────────

_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _quote(text: str) -> str:
    """A TOML basic (double-quoted, single-line) string."""
    out: list[str] = []
    for ch in text:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or ch == "\x7f":
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def format_value(v: object) -> str:
    """Render one value as TOML. Supports what Settings holds: bools, ints,
    floats, strings and flat lists of those."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return _quote(v)
    if isinstance(v, (tuple, list)):
        return "[" + ", ".join(format_value(item) for item in v) + "]"
    raise TypeError(f"cannot write a {type(v).__name__} to config.toml")


# ─── Editing the text ────────────────────────────────────────────────────────────


def _key_pattern(key: str) -> re.Pattern[str]:
    # An uncommented assignment of exactly this key. Anchored per line.
    return re.compile(rf"^\s*{re.escape(key)}\s*=", re.MULTILINE)


def _find_line(lines: list[str], key: str) -> int | None:
    pattern = _key_pattern(key)
    for i, line in enumerate(lines):
        if pattern.match(line):
            return i
    return None


def _edit(text: str, values: dict[str, object], remove: Iterable[str]) -> str:
    lines = text.split("\n")
    to_append: list[tuple[str, object]] = []

    for key, value in values.items():
        index = _find_line(lines, key)
        if index is None and key == "trigger_keys":
            # The single-key setting this replaced: rewrite it under its new
            # name rather than leaving two lines that disagree.
            index = _find_line(lines, LEGACY_TRIGGER_KEY)
        if index is None:
            to_append.append((key, value))
        else:
            lines[index] = f"{key} = {format_value(value)}"

    for key in remove:
        pattern = _key_pattern(key)
        lines = [line for line in lines if not pattern.match(line)]

    if to_append:
        # Keep the file ending in exactly one newline before adding to it.
        while lines and lines[-1].strip() == "":
            lines.pop()
        if APPEND_HEADER not in lines:
            lines.extend(["", APPEND_HEADER])
        for key, value in to_append:
            lines.append(f"{key} = {format_value(value)}")
        lines.append("")

    return "\n".join(lines)


# ─── Checking the result ─────────────────────────────────────────────────────────


def _loose_equal(a: object, b: object) -> bool:
    """Equal for our purposes: tuple vs list and int vs float do not matter,
    but a bool is never equal to a number."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_loose_equal(x, y) for x, y in zip(a, b))
    return a == b


def _verify(text: str, values: dict[str, object], remove: Iterable[str]) -> bool:
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    for key, value in values.items():
        if key not in parsed or not _loose_equal(parsed[key], value):
            return False
    if "trigger_keys" in values and LEGACY_TRIGGER_KEY in parsed:
        return False
    return all(key not in parsed for key in remove)


def _parse_existing(text: str) -> dict[str, object]:
    """The user's current settings, for carrying into a regenerated file.
    Only known fields are kept; anything else was already being ignored."""
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}
    known = {f.name for f in fields(cfg.Settings)}
    out: dict[str, object] = {}
    for key, value in parsed.items():
        if key == LEGACY_TRIGGER_KEY:
            key = "trigger_keys"
        if key in known:
            out[key] = value
    return out


# ─── Writing ─────────────────────────────────────────────────────────────────────


def _write_atomic(path: Path, text: str, previous: str | None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        )
        tmp = Path(handle.name)
        try:
            with handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            if previous is not None:
                path.with_name(path.name + ".bak").write_text(previous, encoding="utf-8")
                try:
                    shutil.copymode(path, tmp)
                except OSError:
                    pass
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    except OSError as exc:
        raise ConfigWriteError(f"could not write {path}: {exc}") from exc


def update_config(
    path: Path, values: dict[str, object], *, remove: Iterable[str] = ()
) -> str:
    """Set `values` in the config file at `path`, deleting the keys in
    `remove`, and return the text that was written.

    A missing file starts from the commented template, so the first save still
    produces a file worth reading. Comments, ordering and untouched lines are
    preserved. If the in-place edit cannot be verified — the user had written a
    multi-line array, say — the file is regenerated from the template with
    their settings carried over. The previous content goes to `<path>.bak`.
    """
    path = Path(path)
    remove = [k for k in remove if k not in values]

    previous: str | None
    try:
        previous = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        previous = None
    except OSError as exc:
        raise ConfigWriteError(f"could not read {path}: {exc}") from exc

    base = cfg.TEMPLATE if previous is None else previous
    text = _edit(base, values, remove)

    if not _verify(text, values, remove):
        merged = _parse_existing(base)
        for key in remove:
            merged.pop(key, None)
        merged.update(values)
        text = _edit(cfg.TEMPLATE, merged, remove)
        if not _verify(text, values, remove):  # pragma: no cover - template bug
            raise ConfigWriteError(f"could not produce a valid {path}")

    _write_atomic(path, text, previous)
    return text
