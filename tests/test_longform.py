"""longform: the pure heuristics, the renderers, and the worker against a fake backend."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from whisperlocal.transcription import longform
from whisperlocal.transcription.backends import BackendError, Segment, TranscriptResult, Word
from whisperlocal.transcription.longform import (
    LongformTranscriber,
    SpeakerSegment,
    clean_segments,
    degenerate,
    fmt_ts,
    merge_tracks,
    paragraphs,
    render_markdown,
    render_text,
    rms_db,
)

KW = dict(max_word_run=4, max_repeat_ratio=0.30, repeat_min_words=5)


def result(*segments: Segment, text: str | None = None) -> TranscriptResult:
    return TranscriptResult(
        text=text if text is not None else " ".join(s.text.strip() for s in segments),
        segments=tuple(segments),
        language=None,
        backend="fake",
        model="fake-model",
        elapsed_ms=1.0,
    )


# ─── degenerate / clean ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("ARP " * 20, True),
        ("funny " * 223, True),
        ("On 25 25 25 25 25", True),
        ("no no no I really do not think that is right", False),
        ("Hello, hello, hello.", False),
        ("", False),
        ("the cat sat on the mat and the dog did too", False),
        ("a b a b a b a b a b a b a b a b a b a b a b", True),  # ratio 2/22
    ],
)
def test_degenerate(text, expected):
    assert degenerate(text, **KW) is expected


def test_clean_segments_filters():
    r = result(
        Segment(0, 1, "  Real words here. "),
        Segment(1, 2, "   "),
        Segment(2, 3, " Thank you. "),
        Segment(3, 4, "you"),
        Segment(4, 5, "Subtitles by the Amara.org community"),
        Segment(5, 6, "quiet mumble", no_speech_prob=0.9),
        Segment(6, 7, "kept despite prob", no_speech_prob=0.5),
        Segment(7, 8, "ARP " * 20),
        Segment(8, 9, "Another real one", words=(Word("Another", 8, 8.5),), no_speech_prob=0.1),
    )
    out = clean_segments(r, **KW)
    assert [s.text for s in out] == ["Real words here.", "kept despite prob", "Another real one"]
    assert out[2].words == (Word("Another", 8, 8.5),)
    assert out[2].no_speech_prob == 0.1
    assert clean_segments(r, no_speech_threshold=0.95, **KW)[1].text == "quiet mumble"


# ─── merge / paragraphs ──────────────────────────────────────────────────────────


def test_merge_tracks_orders_and_labels():
    mic = [Segment(5, 7, " me second "), Segment(0, 2, "me first")]
    sys = [Segment(2, 4, "them"), Segment(5, 6, "them too")]
    out = merge_tracks(mic, sys)
    assert [(s.start, s.speaker, s.track, s.text) for s in out] == [
        (0, "You", "mic", "me first"),
        (2, "Others", "sys", "them"),
        (5, "You", "mic", "me second"),
        (5, "Others", "sys", "them too"),
    ]
    assert all(isinstance(s, SpeakerSegment) for s in out)


def test_merge_tracks_bleed_guard():
    mic = [Segment(0, 2, "really me"), Segment(10, 12, "bleed from speakers"), Segment(20, 21, "tie")]
    sys = [Segment(10, 12, "the other side")]
    mic_rms = {0: -20.0, 1: -20.0, 10: -40.0, 11: -40.0, 20: -30.0}
    sys_rms = {0: -50.0, 1: -50.0, 10: -25.0, 11: -25.0, 20: -24.0}

    out = merge_tracks(mic, sys, mic_rms=mic_rms, sys_rms=sys_rms, bleed_margin_db=6.0)
    assert [s.text for s in out] == ["really me", "the other side", "tie"]

    # Only one map: no guard.
    assert len(merge_tracks(mic, sys, mic_rms=mic_rms)) == 4
    # Unknown seconds: no guard for that segment.
    assert len(merge_tracks(mic, sys, mic_rms={}, sys_rms={})) == 4


def test_paragraphs_merge_same_speaker_within_gap():
    segs = [
        SpeakerSegment(0, 1, "You", "mic", "one"),
        SpeakerSegment(2, 3, "You", "mic", "two"),
        SpeakerSegment(3.5, 4, "Others", "sys", "three"),
        SpeakerSegment(4.5, 5, "Others", "sys", "four"),
        SpeakerSegment(10, 11, "Others", "sys", "five"),
    ]
    out = paragraphs(segs, gap_s=2.0)
    assert [(s.start, s.end, s.speaker, s.text) for s in out] == [
        (0, 3, "You", "one two"),
        (3.5, 5, "Others", "three four"),
        (10, 11, "Others", "five"),
    ]
    assert paragraphs([]) == []


# ─── render ──────────────────────────────────────────────────────────────────────


def test_fmt_ts():
    assert fmt_ts(0) == "00:00:00"
    assert fmt_ts(754.9) == "00:12:34"
    assert fmt_ts(3661) == "01:01:01"
    assert fmt_ts(-3) == "00:00:00"


def test_render_markdown_and_text():
    segs = [
        SpeakerSegment(754, 756, "You", "mic", "hello there"),
        SpeakerSegment(757, 758, "You", "mic", "again"),
        SpeakerSegment(800, 801, "Others", "sys", "hi | pipe"),
    ]
    md = render_markdown(
        "Zoom — standup",
        {"started_at": "2026-09-12 10:00", "duration_s": 1500, "app": "Zoom", "backend": "local", "model": "m", "words": 5},
        segs,
    )
    lines = md.splitlines()
    assert lines[0] == "# Zoom — standup"
    assert "| Started | 2026-09-12 10:00 |" in lines
    assert "| Duration | 25m 00s |" in lines
    assert "| App | Zoom |" in lines
    assert "| Engine | local / m |" in lines
    assert "| Words | 5 |" in lines
    assert "**[00:12:34] You:** hello there again" in lines
    assert "**[00:13:20] Others:** hi | pipe" in lines
    assert md.endswith("\n")

    txt = render_text(segs)
    assert txt == "[00:12:34] You: hello there again\n[00:13:20] Others: hi | pipe\n"

    # Words derived when not given; missing meta rows omitted.
    md2 = render_markdown("T", {}, segs)
    assert "| Words | 6 |" in md2 and "| App |" not in md2


def test_rms_db():
    assert rms_db([]) == -120.0
    assert rms_db([0.0, 0.0, 0.0]) == -120.0
    assert rms_db([1.0, -1.0]) == pytest.approx(0.0)
    assert rms_db([0.5, -0.5]) == pytest.approx(-6.02, abs=0.01)
    assert rms_db([[0.5, 0.5], [-0.5, -0.5]]) == pytest.approx(-6.02, abs=0.01)


def test_is_silent_on_real_file(tmp_path):
    sf = pytest.importorskip("soundfile")
    np = pytest.importorskip("numpy")
    quiet = tmp_path / "quiet.wav"
    loud = tmp_path / "loud.wav"
    sf.write(quiet, np.zeros(16000, dtype="float32"), 16000)
    sf.write(loud, (np.sin(np.arange(16000) * 0.1) * 0.5).astype("float32"), 16000)
    assert longform.is_silent(quiet, -45.0) is True
    assert longform.is_silent(loud, -45.0) is False
    assert longform.is_silent(tmp_path / "missing.wav", -45.0) is False


# ─── worker ──────────────────────────────────────────────────────────────────────


@dataclass
class FakeSettings:
    max_word_run: int = 4
    max_repeat_ratio: float = 0.30
    repeat_min_words: int = 5
    meeting_silence_db: float = -45.0


class FakeBackend:
    name = "fake"
    model = "fake-model"

    def __init__(self, fail_on: set[int] | None = None):
        self.fail_on = fail_on or set()
        self.calls = []
        self.lock = threading.Lock()

    def transcribe_file(self, path, *, language, timestamps):
        with self.lock:
            self.calls.append((Path(path).name, language, timestamps))
        n = int(Path(path).stem.split("-")[-1])
        if n in self.fail_on:
            raise BackendError(f"boom {n}")
        return result(
            Segment(0.0, 1.0, f" chunk {n} words "),
            Segment(1.0, 2.0, "Thank you."),
            Segment(2.0, 3.0, "ARP " * 20),
        )


@pytest.fixture
def not_silent(monkeypatch):
    monkeypatch.setattr(longform, "is_silent", lambda path, threshold: False)


def make_files(tmp_path, n):
    files = []
    for i in range(n):
        p = tmp_path / f"mic-{i}.wav"
        p.write_bytes(b"x")
        files.append(p)
    return files


def test_worker_processes_in_order_with_shift(tmp_path, not_silent):
    backend = FakeBackend()
    results = []
    lt = LongformTranscriber(
        backend, settings=FakeSettings(), on_result=lambda i, t, s: results.append((i, t, s)), language="en"
    )
    for i, p in enumerate(make_files(tmp_path, 3)):
        lt.submit(i, "mic", p, offset_s=60.0 * i)
    assert lt.drain(timeout=5)
    lt.close()

    assert [r[0] for r in results] == [0, 1, 2]
    assert all(r[1] == "mic" for r in results)
    assert backend.calls == [("mic-0.wav", "en", True), ("mic-1.wav", "en", True), ("mic-2.wav", "en", True)]
    for i, (_, _, segs) in enumerate(results):
        assert [s.text for s in segs] == [f"chunk {i} words"]  # phantom + loop dropped
        assert segs[0].start == pytest.approx(60.0 * i)
        assert segs[0].end == pytest.approx(60.0 * i + 1.0)
    assert lt.processed == 3 and lt.failed == 0


def test_worker_respects_should_wait(tmp_path, not_silent):
    backend = FakeBackend()
    results = []
    gate = threading.Event()  # set -> may proceed
    lt = LongformTranscriber(
        backend,
        settings=FakeSettings(),
        on_result=lambda i, t, s: results.append(i),
        should_wait=lambda: not gate.is_set(),
    )
    (p,) = make_files(tmp_path, 1)
    lt.submit(0, "sys", p, 0.0)
    assert not lt.drain(timeout=0.6)
    assert backend.calls == [] and results == []
    gate.set()
    assert lt.drain(timeout=5)
    assert results == [0]
    lt.close()


def test_worker_reports_empty_on_backend_error(tmp_path, not_silent, capsys):
    backend = FakeBackend(fail_on={1})
    results = []
    lt = LongformTranscriber(backend, settings=FakeSettings(), on_result=lambda i, t, s: results.append((i, s)))
    for i, p in enumerate(make_files(tmp_path, 3)):
        lt.submit(i, "mic", p, 0.0)
    assert lt.drain(timeout=5)
    lt.close()
    assert [i for i, _ in results] == [0, 1, 2]
    assert results[1][1] == []
    assert len(results[0][1]) == 1 and len(results[2][1]) == 1
    assert "boom 1" in capsys.readouterr().out
    assert lt.failed == 1


def test_worker_skips_silent_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(longform, "is_silent", lambda path, threshold: threshold == -45.0)
    backend = FakeBackend()
    results = []
    lt = LongformTranscriber(backend, settings=FakeSettings(), on_result=lambda i, t, s: results.append((i, s)))
    (p,) = make_files(tmp_path, 1)
    lt.submit(0, "mic", p, 0.0)
    assert lt.drain(timeout=5)
    lt.close()
    assert results == [(0, [])]
    assert backend.calls == []


def test_close_then_submit_rejected(tmp_path, not_silent):
    lt = LongformTranscriber(FakeBackend(), settings=FakeSettings(), on_result=lambda *a: None)
    lt.close()
    with pytest.raises(RuntimeError):
        lt.submit(0, "mic", tmp_path / "x.wav", 0.0)
    assert lt.drain(timeout=0.1)
    _ = time  # keep the import used for editors that strip it
