"""
Noticing that a meeting is happening.

The signal is CoreAudio's per-process "has input running" flag — the same
thing that lights the orange dot in the menu bar — filtered to processes we
recognise as meeting apps. That is app-agnostic enough to cover Zoom, Teams,
FaceTime, Slack huddles, Webex, Discord and, through the browser helper
processes, Google Meet and friends. A browser call gets its name from the
frontmost window title when Accessibility lets us read it.

Polling runs on a rumps.Timer, i.e. the main thread, because it touches
NSWorkspace and Accessibility. Each tick is around a millisecond. Anything
that follows a detection (a notification, starting a recording) is handed
off; nothing slow happens here.

    NO_MEETING ──(2 ticks positive)──▶ DETECTED ──▶ PROMPTED / RECORDING
        ▲                                 │                │
        └──(grace period without input)── ENDING ◀─────────┘
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from whisperlocal.config import Settings

# bundle-id prefix -> (config key, display name)
MEETING_APPS: dict[str, tuple[str, str]] = {
    "us.zoom.xos": ("zoom", "Zoom"),
    "com.microsoft.teams": ("teams", "Microsoft Teams"),
    "com.microsoft.teams2": ("teams", "Microsoft Teams"),
    "com.apple.FaceTime": ("facetime", "FaceTime"),
    "com.apple.avconferenced": ("facetime", "FaceTime"),
    "com.tinyspeck.slackmacgap": ("slack", "Slack"),
    "Cisco-Systems.Spark": ("webex", "Webex"),
    "com.webex.meetingmanager": ("webex", "Webex"),
    "com.hnc.Discord": ("discord", "Discord"),
    # Browser helper processes: the tab that has the mic open lives here.
    "com.google.Chrome": ("browser", "Chrome"),
    "com.apple.WebKit": ("browser", "Safari"),
    "com.apple.Safari": ("browser", "Safari"),
    "company.thebrowser.Browser": ("browser", "Arc"),
    "com.brave.Browser": ("browser", "Brave"),
    "com.microsoft.edgemac": ("browser", "Edge"),
    "org.mozilla.firefox": ("browser", "Firefox"),
    "org.mozilla.plugincontainer": ("browser", "Firefox"),
    "ai.perplexity.comet": ("browser", "Comet"),
    "com.vivaldi.Vivaldi": ("browser", "Vivaldi"),
}

# Words in a browser window title that name the call.
BROWSER_CALL_HINTS = (
    ("meet.google.com", "Google Meet"),
    ("google meet", "Google Meet"),
    ("meet -", "Google Meet"),
    ("teams", "Microsoft Teams"),
    ("zoom", "Zoom"),
    ("whereby", "Whereby"),
    ("webex", "Webex"),
    ("jitsi", "Jitsi"),
    ("huddle", "Slack huddle"),
    ("around", "Around"),
)


def classify_bundle(bundle_id: str) -> tuple[str, str] | None:
    for prefix, info in MEETING_APPS.items():
        if bundle_id == prefix or bundle_id.startswith(prefix + "."):
            return info
    return None


@dataclass
class DetectedMeeting:
    app: str
    key: str
    bundle_id: str
    pid: int
    title: str | None = None
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    @property
    def label(self) -> str:
        return self.title or self.app


class MeetingDetector:
    NO_MEETING = "no_meeting"
    DETECTED = "detected"
    PROMPTED = "prompted"
    DISMISSED = "dismissed"
    RECORDING = "recording"
    ENDING = "ending"

    INTERVAL = 5  # seconds between ticks
    CONFIRM_TICKS = 2  # positive ticks before a meeting counts

    def __init__(
        self,
        settings: Settings,
        *,
        on_detected: Callable[[DetectedMeeting], None],
        on_ended: Callable[[DetectedMeeting], None],
        on_state: Callable[[str, DetectedMeeting | None], None] | None = None,
    ):
        self.settings = settings
        self.on_detected = on_detected
        self.on_ended = on_ended
        self.on_state = on_state
        self.state = self.NO_MEETING
        self.current: DetectedMeeting | None = None
        self._positive_ticks = 0
        self._quiet_since: float | None = None
        self._timer = None
        self._probe_failures = 0

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Begin polling. Main thread (creates a rumps.Timer)."""
        if self._timer is not None:
            return
        import rumps

        self._timer = rumps.Timer(self._tick, self.INTERVAL)
        self._timer.start()
        print("Meeting detection started")

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
            print("Meeting detection stopped")
        self._set_state(self.NO_MEETING, None)

    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        if settings.meeting_enabled and self._timer is None:
            self.start()
        elif not settings.meeting_enabled and self._timer is not None:
            self.stop()

    # ── external nudges ──────────────────────────────────────────────────

    def notify_recording_started(self) -> None:
        if self.current is not None:
            self._set_state(self.RECORDING, self.current)

    def notify_recording_stopped(self) -> None:
        if self.state == self.RECORDING:
            # Stopped by hand while the call goes on: do not ask again.
            self._set_state(self.DISMISSED if self.current else self.NO_MEETING, self.current)

    def dismiss(self) -> None:
        if self.current is not None:
            self._set_state(self.DISMISSED, self.current)

    def mark_prompted(self) -> None:
        if self.current is not None:
            self._set_state(self.PROMPTED, self.current)

    # ── polling ──────────────────────────────────────────────────────────

    def _tick(self, _timer=None) -> None:
        try:
            found = self.probe()
        except Exception as exc:
            self._probe_failures += 1
            if self._probe_failures <= 3:
                print(f"Warning: meeting probe failed: {exc}")
            return
        self._advance(found)

    def probe(self) -> DetectedMeeting | None:
        """One look at the system: is a known meeting app using the mic?"""
        from whisperlocal import systemaudio as sa

        wanted = set(self.settings.meeting_apps)
        candidates: list[DetectedMeeting] = []
        for proc in sa.process_objects():
            if not proc.running_input or not proc.bundle_id:
                continue
            info = classify_bundle(proc.bundle_id)
            if info is None or info[0] not in wanted:
                continue
            key, name = info
            candidates.append(DetectedMeeting(name, key, proc.bundle_id, proc.pid))
        if not candidates:
            return None
        # A native meeting app wins over a browser tab, since browsers also
        # hold the mic open for plenty of things that are not calls.
        candidates.sort(key=lambda c: c.key == "browser")
        best = candidates[0]
        if best.key == "browser":
            best.title = browser_call_title(best.pid) or best.title
        return best

    def _advance(self, found: DetectedMeeting | None) -> None:
        now = time.time()
        if found is not None:
            self._quiet_since = None
            if self.current is None or self.current.bundle_id != found.bundle_id:
                if self.state in (self.RECORDING,):
                    # Another meeting app took over mid-recording; keep going.
                    self.current.last_seen = now
                    return
                self._positive_ticks = 1
                self.current = found
                self._set_state(self.NO_MEETING, found)
                return
            self.current.last_seen = now
            if found.title and not self.current.title:
                self.current.title = found.title
            self._positive_ticks += 1
            if self.state == self.NO_MEETING and self._positive_ticks >= self.CONFIRM_TICKS:
                self._set_state(self.DETECTED, self.current)
                print(f"Meeting detected: {self.current.label} ({self.current.bundle_id} pid {self.current.pid})")
                self.on_detected(self.current)
            elif self.state == self.ENDING:
                # It came back within the grace period — false alarm.
                self._set_state(self.RECORDING if self._was_recording else self.DETECTED, self.current)
            return

        # Nothing running input.
        if self.current is None or self.state == self.NO_MEETING:
            self._positive_ticks = 0
            self.current = None
            return
        if self._quiet_since is None:
            self._quiet_since = now
            self._was_recording = self.state == self.RECORDING
            self._set_state(self.ENDING, self.current)
            return
        if now - self._quiet_since >= self.settings.meeting_end_grace_seconds:
            ended = self.current
            print(f"Meeting ended ({ended.label}, no input for {self.settings.meeting_end_grace_seconds}s)")
            self.current = None
            self._positive_ticks = 0
            self._quiet_since = None
            self._set_state(self.NO_MEETING, None)
            if self._was_recording:
                self.on_ended(ended)

    _was_recording = False

    def _set_state(self, state: str, meeting: DetectedMeeting | None) -> None:
        if state == self.state and meeting is self.current:
            return
        self.state = state
        if self.on_state:
            try:
                self.on_state(state, meeting)
            except Exception as exc:
                print(f"Warning: meeting state handler failed: {exc}")


# ─── Browser window titles ───────────────────────────────────────────────────────


def browser_call_title(pid: int) -> str | None:
    """Best-effort: the focused window title of the browser owning `pid`'s
    helper, if it looks like a call. Needs Accessibility; None otherwise."""
    try:
        import AppKit
        from ApplicationServices import (
            AXUIElementCopyAttributeValue,
            AXUIElementCreateApplication,
            AXUIElementSetMessagingTimeout,
            kAXFocusedWindowAttribute,
            kAXTitleAttribute,
        )
    except ImportError:
        return None

    try:
        # Helper processes have no windows; find the browser they belong to
        # by bundle-id prefix among running applications.
        helper = None
        for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
            if app.processIdentifier() == pid:
                helper = app
                break
        bundle = (helper.bundleIdentifier() if helper else "") or ""
        owner_prefix = bundle.split(".helper")[0].split(".Helper")[0]
        pids = [pid]
        for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
            bid = app.bundleIdentifier() or ""
            if bid and (bid == owner_prefix or owner_prefix.startswith(bid)) and app.processIdentifier() != pid:
                pids.insert(0, app.processIdentifier())
        for candidate in pids:
            element = AXUIElementCreateApplication(candidate)
            AXUIElementSetMessagingTimeout(element, 0.25)
            err, window = AXUIElementCopyAttributeValue(element, kAXFocusedWindowAttribute, None)
            if err or window is None:
                continue
            err, title = AXUIElementCopyAttributeValue(window, kAXTitleAttribute, None)
            if err or not title:
                continue
            text = str(title)
            lowered = text.lower()
            for needle, name in BROWSER_CALL_HINTS:
                if needle in lowered:
                    return name
            return text[:60] if text else None
    except Exception:
        return None
    return None
