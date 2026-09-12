"""Shared fixtures. Everything here must run on Linux CI: no AppKit, no audio."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """Point config_path() at a scratch file and return it."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from whisperlocal import config as cfg

    return cfg.config_path()
