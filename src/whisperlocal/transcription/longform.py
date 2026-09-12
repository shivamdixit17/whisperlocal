"""
Long-form (meeting) transcription: cleaning, merging and rendering.

Everything above LongformTranscriber is a pure function of its inputs so the
heuristics can be tested without audio. LongformTranscriber itself is the
background worker that feeds recorded chunks through a backend one at a
time, yielding to push-to-talk, which is the interactive workload.

No AppKit, numpy or MLX at import time: soundfile is imported lazily inside
is_silent, and a missing soundfile simply means nothing is treated as silent.
"""

from __future__ import annotations

import math
import queue
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from whisperlocal.transcription.backends import Segment, TranscriptResult

# Whisper's favourite hallucinations on silence, lowercase and stripped. These
# are what the model emits when a chunk has no speech in it: YouTube training
# data leaking out.
PHANTOMS = frozenset(
    {
        "thank you.",
        "thanks for watching.",
        "thank you for watching.",
        "you",
        "bye.",
        "subtitles by the amara.org community",
    }
)

SPEAKER_MIC = "You"
SPEAKER_SYS = "Others"
TRACK_SPEAKERS = {"mic": SPEAKER_MIC, "sys": SPEAKER_SYS}


# ─── Cleaning ────────────────────────────────────────────────────────────────────


def degenerate(text: str, max_word_run: int, max_repeat_ratio: float, repeat_min_words: int) -> bool:
    """True if `text` looks like a decoder repetition loop rather than speech.

    The same logic as app.looks_degenerate, without the Settings dependency.
    Two checks: a long run of one word back to back (short loops) and a low
    unique-word ratio (long loops). Tuned so "Hello, hello, hello." and
    "no no no I really do not think that is right" both survive.
    """
    words = [w.lower().strip(".,!?;:") for w in text.split()]
    if not words:
        return False

    run = best = 1
    for a, b in zip(words, words[1:]):
        run = run + 1 if a == b else 1
        best = max(best, run)
    if best >= max_word_run:
        return True

    if len(words) < repeat_min_words:
        return False
    return len(set(words)) / len(words) < max_repeat_ratio


def is_phantom(text: str) -> bool:
    return text.strip().lower() in PHANTOMS


def clean_segments(
    result: TranscriptResult,
    *,
    max_word_run: int,
    max_repeat_ratio: float,
    repeat_min_words: int,
    no_speech_threshold: float = 0.6,
) -> list[Segment]:
    """Drop the segments that are not speech; strip the text of the rest.

    Gone: empty text, the phantom phrases, segments whisper itself marked as
    probably-not-speech, and repetition loops.
    """
    kept: list[Segment] = []
    for seg in result.segments:
        text = seg.text.strip()
        if not text:
            continue
        if is_phantom(text):
            continue
        if seg.no_speech_prob is not None and seg.no_speech_prob > no_speech_threshold:
            continue
        if degenerate(text, max_word_run, max_repeat_ratio, repeat_min_words):
            continue
        kept.append(
            Segment(
                start=seg.start,
                end=seg.end,
                text=text,
                words=seg.words,
                no_speech_prob=seg.no_speech_prob,
            )
        )
    return kept


# ─── Merging tracks ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SpeakerSegment:
    start: float
    end: float
    speaker: str
    track: str
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "speaker": self.speaker,
            "track": self.track,
            "text": self.text,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SpeakerSegment":
        return cls(
            start=float(d.get("start", 0.0)),
            end=float(d.get("end", 0.0)),
            speaker=str(d.get("speaker", "")),
            track=str(d.get("track", "")),
            text=str(d.get("text", "")),
        )


def _window_level(rms: dict[int, float], start: float, end: float) -> float | None:
    """Mean dB level over the whole seconds a segment spans, or None if unknown."""
    lo = int(math.floor(start))
    hi = max(lo, int(math.ceil(end)) - 1)
    levels = [rms[s] for s in range(lo, hi + 1) if s in rms]
    if not levels:
        return None
    return sum(levels) / len(levels)


def merge_tracks(
    mic: list[Segment],
    sys: list[Segment],
    *,
    mic_rms: dict[int, float] | None = None,
    sys_rms: dict[int, float] | None = None,
    bleed_margin_db: float = 6.0,
) -> list[SpeakerSegment]:
    """Interleave the two tracks by time, labelling who spoke.

    With both RMS maps (dB per whole second, keyed by int(start)) a mic
    segment whose window was clearly louder on the system track is dropped:
    that is the other side's voice bleeding out of the speakers into the mic,
    and the system track already has the words. A tie keeps both.
    """
    out: list[SpeakerSegment] = []
    guard = mic_rms is not None and sys_rms is not None
    for seg in mic:
        if guard:
            mic_level = _window_level(mic_rms, seg.start, seg.end)
            sys_level = _window_level(sys_rms, seg.start, seg.end)
            if (
                mic_level is not None
                and sys_level is not None
                and sys_level > mic_level + bleed_margin_db
            ):
                continue
        out.append(SpeakerSegment(seg.start, seg.end, SPEAKER_MIC, "mic", seg.text.strip()))
    for seg in sys:
        out.append(SpeakerSegment(seg.start, seg.end, SPEAKER_SYS, "sys", seg.text.strip()))
    # Stable sort on start only: equal starts keep mic before sys.
    out.sort(key=lambda s: s.start)
    return out


def paragraphs(segments: list[SpeakerSegment], gap_s: float = 2.0) -> list[SpeakerSegment]:
    """Join consecutive same-speaker segments separated by at most `gap_s`."""
    out: list[SpeakerSegment] = []
    for seg in segments:
        if out:
            prev = out[-1]
            if prev.speaker == seg.speaker and seg.start - prev.end <= gap_s:
                out[-1] = SpeakerSegment(
                    start=prev.start,
                    end=max(prev.end, seg.end),
                    speaker=prev.speaker,
                    track=prev.track,
                    text=f"{prev.text} {seg.text}".strip(),
                )
                continue
        out.append(seg)
    return out


# ─── Rendering ───────────────────────────────────────────────────────────────────


def fmt_ts(seconds: float) -> str:
    """Seconds as HH:MM:SS (floored, never negative)."""
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def word_count(text: str) -> int:
    return len(text.split())


def _md_cell(value: object) -> str:
    return str(value if value is not None else "—").replace("|", "\\|").replace("\n", " ")


def render_markdown(title: str, meta: dict, segments: list[SpeakerSegment]) -> str:
    """A Markdown transcript: title, metadata table, then timestamped lines.

    `meta` may carry started_at, duration_s, app, backend, model, words; any
    of them may be missing.
    """
    paras = paragraphs(segments)
    words = meta.get("words")
    if words is None:
        words = sum(word_count(s.text) for s in segments)
    backend = meta.get("backend")
    model = meta.get("model")
    engine = " / ".join(str(x) for x in (backend, model) if x) or None

    rows = [
        ("Started", meta.get("started_at")),
        ("Duration", fmt_duration(meta.get("duration_s")) if meta.get("duration_s") is not None else None),
        ("App", meta.get("app")),
        ("Engine", engine),
        ("Words", words),
    ]
    lines = [f"# {title.strip() or 'Meeting'}", ""]
    lines.append("| | |")
    lines.append("|---|---|")
    for label, value in rows:
        if value is None or value == "":
            continue
        lines.append(f"| {label} | {_md_cell(value)} |")
    lines.append("")
    for seg in paras:
        lines.append(f"**[{fmt_ts(seg.start)}] {seg.speaker}:** {seg.text}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_text(segments: Iterable[SpeakerSegment]) -> str:
    paras = paragraphs(list(segments))
    return "".join(f"[{fmt_ts(s.start)}] {s.speaker}: {s.text}\n" for s in paras)


# ─── Audio level helpers ─────────────────────────────────────────────────────────


def rms_db(samples: Sequence[float]) -> float:
    """RMS level of a sample sequence in dBFS. -120 for empty or all-zero.

    Accepts any sequence of floats, nested one level (frames of channels),
    or anything with tolist() such as a numpy array.
    """
    if hasattr(samples, "tolist"):
        samples = samples.tolist()
    acc = 0.0
    n = 0
    for value in samples:
        if isinstance(value, (list, tuple)):
            for v in value:
                acc += float(v) * float(v)
                n += 1
        else:
            acc += float(value) * float(value)
            n += 1
    if n == 0 or acc <= 0.0:
        return -120.0
    return max(-120.0, 20.0 * math.log10(math.sqrt(acc / n)))


def is_silent(path: Path, threshold_db: float) -> bool:
    """True if the file's overall RMS is below `threshold_db`.

    Reads in blocks so an hour of audio does not have to sit in memory. If
    soundfile is not installed, or the file cannot be read, the answer is
    False: better to transcribe a silent chunk than to skip a spoken one.
    """
    try:
        import soundfile as sf
    except ImportError:
        return False
    try:
        acc = 0.0
        n = 0
        with sf.SoundFile(str(path)) as f:
            for block in f.blocks(blocksize=65536, dtype="float32", always_2d=True):
                if block.size == 0:
                    continue
                acc += float((block.astype("float64") ** 2).sum())
                n += block.size
        if n == 0:
            return True
        mean_sq = acc / n
        level = -120.0 if mean_sq <= 0 else 20.0 * math.log10(math.sqrt(mean_sq))
        return level < threshold_db
    except Exception as exc:
        print(f"Warning: could not measure {Path(path).name}: {exc}")
        return False


# ─── Worker ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Job:
    index: int
    track: str
    path: Path
    offset_s: float


class LongformTranscriber:
    """Feeds recorded chunks through a backend on one daemon thread.

    Chunks arrive via submit(index, track, path, offset_s) and are processed
    in order. For each one, on_result(index, track, segments) is called
    exactly once — with an empty list if the chunk was silent or the backend
    failed — so the caller's bookkeeping (which chunks are done) never stalls.
    While should_wait() is true the worker idles: push-to-talk shares the
    model and must not queue behind a minute of meeting audio.
    """

    def __init__(
        self,
        backend,
        *,
        settings,
        on_result: Callable[[int, str, list[Segment]], None],
        should_wait: Callable[[], bool] = lambda: False,
        language: str | None = None,
    ):
        self.backend = backend
        self.settings = settings
        self.on_result = on_result
        self.should_wait = should_wait
        self.language = language
        self._queue: queue.Queue[_Job | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._closed = False
        self.processed = 0
        self.failed = 0

    # ── public ───────────────────────────────────────────────────────────────

    def submit(self, index: int, track: str, path: Path, offset_s: float) -> None:
        if self._closed:
            raise RuntimeError("LongformTranscriber is closed")
        self._idle.clear()
        self._queue.put(_Job(index, track, Path(path), float(offset_s)))
        self._ensure_worker()

    def pending(self) -> int:
        return self._queue.qsize()

    def drain(self, timeout: float | None = None) -> bool:
        """Block until every submitted job has been handled. False on timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._queue.empty() and self._idle.is_set():
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            remaining = 0.05 if deadline is None else min(0.05, max(0.0, deadline - time.monotonic()))
            self._idle.wait(remaining)

    def close(self) -> None:
        """Stop the worker after the current job. Pending jobs are dropped."""
        self._closed = True
        thread = self._thread
        if thread is not None:
            self._queue.put(None)
            thread.join(timeout=5.0)

    # ── internals ────────────────────────────────────────────────────────────

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, name="whisperlocal-longform", daemon=True
            )
            self._thread.start()

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job is None:
                    # Drop whatever is queued behind the sentinel.
                    while True:
                        try:
                            self._queue.get_nowait()
                        except queue.Empty:
                            break
                    return
                self._process(job)
            finally:
                self._queue.task_done()
                if self._queue.empty():
                    self._idle.set()

    def _process(self, job: _Job) -> None:
        while self.should_wait() and not self._closed:
            time.sleep(0.2)

        segments: list[Segment] = []
        try:
            threshold = float(getattr(self.settings, "meeting_silence_db", -45.0))
            if is_silent(job.path, threshold):
                print(f"   (chunk {job.index} {job.track}: silent, skipped)")
            else:
                result = self.backend.transcribe_file(
                    job.path, language=self.language, timestamps=True
                ).shifted(job.offset_s)
                segments = clean_segments(
                    result,
                    max_word_run=int(getattr(self.settings, "max_word_run", 4)),
                    max_repeat_ratio=float(getattr(self.settings, "max_repeat_ratio", 0.30)),
                    repeat_min_words=int(getattr(self.settings, "repeat_min_words", 5)),
                )
            self.processed += 1
        except Exception as exc:
            self.failed += 1
            print(f"Error: chunk {job.index} ({job.track}) failed: {exc}")
            traceback.print_exc()
            segments = []
        try:
            self.on_result(job.index, job.track, segments)
        except Exception as exc:
            print(f"Error: on_result for chunk {job.index} raised: {exc}")
            traceback.print_exc()


__all__ = [
    "LongformTranscriber",
    "PHANTOMS",
    "SPEAKER_MIC",
    "SPEAKER_SYS",
    "SpeakerSegment",
    "clean_segments",
    "degenerate",
    "fmt_duration",
    "fmt_ts",
    "is_phantom",
    "is_silent",
    "merge_tracks",
    "paragraphs",
    "render_markdown",
    "render_text",
    "rms_db",
    "word_count",
]
