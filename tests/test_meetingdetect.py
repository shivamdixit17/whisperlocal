"""Meeting detection state machine, with a fake probe (no CoreAudio needed)."""

from __future__ import annotations

import pytest

from whisperlocal import meetingdetect as md
from whisperlocal.config import Settings


def test_classify_bundle():
    assert md.classify_bundle("us.zoom.xos") == ("zoom", "Zoom")
    assert md.classify_bundle("com.google.Chrome.helper") == ("browser", "Chrome")
    assert md.classify_bundle("com.apple.WebKit.GPU") == ("browser", "Safari")
    assert md.classify_bundle("com.spotify.client") is None


class FakeDetector(md.MeetingDetector):
    """Replace the CoreAudio probe with a scripted answer."""

    def __init__(self, settings, **kw):
        super().__init__(settings, **kw)
        self.answer = None

    def probe(self):
        return self.answer


@pytest.fixture
def detector(monkeypatch):
    events = {"detected": [], "ended": [], "states": []}
    now = [1000.0]
    monkeypatch.setattr(md.time, "time", lambda: now[0])
    d = FakeDetector(
        Settings(meeting_end_grace_seconds=20),
        on_detected=lambda m: events["detected"].append(m),
        on_ended=lambda m: events["ended"].append(m),
        on_state=lambda s, m: events["states"].append(s),
    )
    return d, events, now


def zoom():
    return md.DetectedMeeting("Zoom", "zoom", "us.zoom.xos", 4123)


def test_two_ticks_confirm_a_meeting(detector):
    d, events, now = detector
    d.answer = zoom()
    d._tick()
    assert events["detected"] == []
    d._tick()
    assert len(events["detected"]) == 1
    assert d.state == md.MeetingDetector.DETECTED


def test_single_blip_is_ignored(detector):
    d, events, now = detector
    d.answer = zoom()
    d._tick()
    d.answer = None
    d._tick()
    assert events["detected"] == []
    assert d.state == md.MeetingDetector.NO_MEETING
    assert d.current is None


def test_recording_ends_after_grace(detector):
    d, events, now = detector
    d.answer = zoom()
    d._tick(); d._tick()
    d.notify_recording_started()
    assert d.state == md.MeetingDetector.RECORDING
    d.answer = None
    d._tick()
    assert d.state == md.MeetingDetector.ENDING
    now[0] += 10
    d._tick()
    assert events["ended"] == []
    now[0] += 15
    d._tick()
    assert len(events["ended"]) == 1
    assert d.state == md.MeetingDetector.NO_MEETING


def test_brief_dropout_does_not_end_recording(detector):
    d, events, now = detector
    d.answer = zoom()
    d._tick(); d._tick()
    d.notify_recording_started()
    d.answer = None
    d._tick()
    d.answer = zoom()
    now[0] += 5
    d._tick()
    assert d.state == md.MeetingDetector.RECORDING
    assert events["ended"] == []


def test_dismissed_meeting_is_not_asked_again(detector):
    d, events, now = detector
    d.answer = zoom()
    d._tick(); d._tick()
    d.dismiss()
    for _ in range(5):
        d._tick()
    assert len(events["detected"]) == 1
    assert d.state == md.MeetingDetector.DISMISSED


def test_ended_without_recording_does_not_call_on_ended(detector):
    d, events, now = detector
    d.answer = zoom()
    d._tick(); d._tick()
    d.answer = None
    d._tick()
    now[0] += 30
    d._tick()
    assert events["ended"] == []
    assert d.state == md.MeetingDetector.NO_MEETING


def test_apps_filter_respected(monkeypatch):
    class P:
        def __init__(self, bid, inp=True):
            self.bundle_id, self.running_input, self.running_output, self.pid, self.object_id = bid, inp, False, 1, 1

    from whisperlocal import systemaudio

    monkeypatch.setattr(
        systemaudio, "process_objects", lambda: [P("us.zoom.xos"), P("com.google.Chrome.helper")]
    )
    d = md.MeetingDetector(Settings(meeting_apps=("browser",)), on_detected=lambda m: None, on_ended=lambda m: None)
    monkeypatch.setattr(md, "browser_call_title", lambda pid: "Google Meet")
    found = d.probe()
    assert found.key == "browser" and found.title == "Google Meet"
    d2 = md.MeetingDetector(Settings(meeting_apps=("zoom", "browser")), on_detected=lambda m: None, on_ended=lambda m: None)
    assert d2.probe().key == "zoom"  # native app wins over a browser tab
