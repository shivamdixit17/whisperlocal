"""WhisperLocal — the local web dashboard.

Everything in this package is stdlib only and imports cleanly on Linux: the
server binds 127.0.0.1, serves the static frontend from ``static/`` and exposes
a small JSON API (``api.py``) over an ``AppContext`` the menu bar app or the
standalone CLI server provides. Nothing here touches AppKit, pynput or audio.
"""
