"""The pure modules must import without the macOS-only stack.

Anything that lists itself here can be tested on Linux CI. Importing
whisperlocal.app pulls in AppKit, pynput and PortAudio and sys.exits if any of
them is missing, so a module that needs it cannot be in this list.
"""

from __future__ import annotations

import importlib
import sys

import pytest

PURE_MODULES = [
    "whisperlocal.meetingdetect",
    "whisperlocal.systemaudio",
    "whisperlocal.analytics",
    "whisperlocal.config",
    "whisperlocal.config_writer",
    "whisperlocal.keychain",
    "whisperlocal.keymap",
    "whisperlocal.meetings",
    "whisperlocal.settings_manager",
    "whisperlocal.stats",
    "whisperlocal.transcription.backends",
    "whisperlocal.transcription.longform",
    "whisperlocal.web.server",
    "whisperlocal.web.api",
    "whisperlocal.web.settings_schema",
]


@pytest.mark.parametrize("name", PURE_MODULES)
def test_imports_without_app(name):
    sys.modules.pop("whisperlocal.app", None)
    importlib.import_module(name)
    assert "whisperlocal.app" not in sys.modules
    assert "AppKit" not in sys.modules or name == "whisperlocal.app"
