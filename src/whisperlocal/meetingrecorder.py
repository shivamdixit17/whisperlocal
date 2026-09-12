"""
Meeting recording: capture, segmenting, live transcription, finalisation.

A meeting is long — an hour is normal — so nothing here holds the audio in
memory. The capture thread writes fixed-size WAV segments as it goes (one per
track: "mic" is you, "sys" is everyone else through the system-audio tap),
and each finished segment is handed to a single worker that transcribes it
while the next one records. A crash loses at most the segment being written.

Layout under MeetingStore's directory for the meeting:

    audio/segments/mic_0003.wav, sys_0003.wav   while recording
    audio/mic.flac, audio/system.flac            after (if meeting_keep_audio)

Threads: the PortAudio drain thread (owned here, never a callback — see
audio.py), the LongformTranscriber worker, and a finaliser thread started by
stop(). Nothing touches AppKit; events are delivered on whichever thread
raised them, and the menu bar marshals them itself.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime
import math
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from whisperlocal import audio
from whisperlocal.config import Settings
from whisperlocal.meetings import MeetingStore
from whisperlocal.transcription.backends import Segment, make_backend
from whisperlocal.transcription.longform import LongformTranscriber, merge_tracks

Event = Callable[[str, dict], None]


# ─── Segment files ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SegmentInfo:
    index: int
    track: str
    path: Path
    start_s: float
    end_s: float


class TrackWriter:
    """Writes one mono track to numbered 16-bit WAV files, one block at a time."""

    def __init__(self, directory: Path, name: str, samplerate: int):
        import soundfile as sf

        self._sf = sf
        self.directory = directory
        self.name = name
        self.samplerate = samplerate
        self.index = 0
        self._file = None
        self._segment_start = 0  # in frames since the recording began
        self._written = 0  # frames in the current segment
        self._total = 0
        directory.mkdir(parents=True, exist_ok=True)

    def _path(self) -> Path:
        return self.directory / f"{self.name}_{self.index:04d}.wav"

    def write(self, block) -> None:
        if self._file is None:
            self._file = self._sf.SoundFile(
                str(self._path()), "w", samplerate=self.samplerate, channels=1, subtype="PCM_16"
            )
            self._segment_start = self._total
            self._written = 0
        self._file.write(block)
        self._written += len(block)
        self._total += len(block)

    @property
    def segment_seconds(self) -> float:
        return self._written / self.samplerate

    def cut(self) -> SegmentInfo | None:
        """Close the current segment. The next write starts a new file."""
        if self._file is None:
            return None
        path = Path(self._file.name)
        self._file.close()
        self._file = None
        info = SegmentInfo(
            self.index,
            self.name,
            path,
            self._segment_start / self.samplerate,
            (self._segment_start + self._written) / self.samplerate,
        )
        self.index += 1
        return info

    def close(self) -> SegmentInfo | None:
        return self.cut()


# ─── Capture ─────────────────────────────────────────────────────────────────────


def _rms_db(block) -> float:
    if block is None or len(block) == 0:
        return -120.0
    power = float((block.astype("float64") ** 2).mean())
    return 20 * math.log10(max(power, 1e-12)) / 2


@dataclass
class CaptureDevice:
    portaudio_index: int | None  # None = default input
    samplerate: int
    channels: int
    mic_channels: int
    has_system: bool
    label: str
    system_index: int | None = None  # a second device carrying system audio (BlackHole style)
    system_channels: int = 0


class MeetingCapture:
    """Reads the capture device on an owned thread and writes segments.

    Reads are polled rather than blocking: a tap aggregate that macOS has not
    been allowed to run never delivers a frame, and a blocking read would sit
    there forever (and make stop() unsafe). If nothing arrives for
    NO_AUDIO_TIMEOUT after start, on_no_audio fires and the recorder falls
    back to the plain microphone.
    """

    BLOCKSIZE = 2048
    SILENCE_WINDOW_S = 0.4
    NO_AUDIO_TIMEOUT = 3.0

    def __init__(
        self,
        device: CaptureDevice,
        segments_dir: Path,
        *,
        segment_seconds: int,
        silence_db: float,
        on_segment: Callable[[SegmentInfo, SegmentInfo | None], None],
        on_no_audio: Callable[[], None] | None = None,
    ):
        self.device = device
        self.on_no_audio = on_no_audio
        self.dead = False
        self.segment_seconds = max(20, segment_seconds)
        self.silence_db = silence_db
        self.on_segment = on_segment
        self.mic = TrackWriter(segments_dir, "mic", device.samplerate)
        self.sys = TrackWriter(segments_dir, "sys", device.samplerate) if device.has_system else None
        self._stream = None
        self._sys_stream = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()
        self.started_at: float | None = None
        self.frames = 0
        # Per-second peak level per track, for the "who was talking" merge.
        self.mic_rms: dict[int, float] = {}
        self.sys_rms: dict[int, float] = {}
        self._recent: collections.deque = collections.deque(
            maxlen=max(1, int(self.SILENCE_WINDOW_S * device.samplerate / self.BLOCKSIZE))
        )

    @property
    def elapsed(self) -> float:
        return self.frames / self.device.samplerate

    def start(self) -> None:
        import sounddevice

        dev = self.device
        with audio.PORTAUDIO_LOCK:
            self._stream = sounddevice.InputStream(
                device=dev.portaudio_index,
                samplerate=dev.samplerate,
                channels=dev.channels,
                dtype="float32",
                blocksize=self.BLOCKSIZE,
            )
            self._stream.start()
            audio._stream_opened()
            if dev.system_index is not None:
                self._sys_stream = sounddevice.InputStream(
                    device=dev.system_index,
                    samplerate=dev.samplerate,
                    channels=dev.system_channels,
                    dtype="float32",
                    blocksize=self.BLOCKSIZE,
                )
                self._sys_stream.start()
                audio._stream_opened()
        self._running = True
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._drain, name="meeting-capture", daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        import numpy as np

        dev = self.device
        while self._running:
            try:
                if self._stream.read_available < self.BLOCKSIZE:
                    if self.frames == 0 and time.time() - self.started_at > self.NO_AUDIO_TIMEOUT:
                        self.dead = True
                        print("Warning: the capture device delivered no audio")
                        if self.on_no_audio:
                            self.on_no_audio()
                        break
                    time.sleep(0.01)
                    continue
                data, overflowed = self._stream.read(self.BLOCKSIZE)
            except Exception as exc:
                print(f"Warning: meeting audio read error: {exc}")
                break
            if overflowed:
                print("Warning: meeting audio buffer overflow")

            mic = data[:, : dev.mic_channels].mean(axis=1) if dev.mic_channels > 1 else data[:, 0]
            sys_block = None
            if self.sys is not None:
                if dev.system_index is not None:
                    try:
                        if self._sys_stream.read_available >= self.BLOCKSIZE:
                            sdata, _ = self._sys_stream.read(self.BLOCKSIZE)
                            sys_block = sdata.mean(axis=1) if sdata.shape[1] > 1 else sdata[:, 0]
                        else:
                            sys_block = np.zeros(len(mic), dtype="float32")
                    except Exception as exc:
                        print(f"Warning: system audio read error: {exc}")
                        sys_block = np.zeros(len(mic), dtype="float32")
                else:
                    tail = data[:, dev.mic_channels :]
                    sys_block = tail.mean(axis=1) if tail.shape[1] > 1 else tail[:, 0]

            second = int(self.frames / dev.samplerate)
            m_db = _rms_db(mic)
            self.mic_rms[second] = max(self.mic_rms.get(second, -120.0), m_db)
            s_db = -120.0
            if sys_block is not None:
                s_db = _rms_db(sys_block)
                self.sys_rms[second] = max(self.sys_rms.get(second, -120.0), s_db)
            self._recent.append(max(m_db, s_db))

            with self._lock:
                self.mic.write(mic.astype("float32", copy=False))
                if self.sys is not None and sys_block is not None:
                    self.sys.write(sys_block.astype("float32", copy=False))
                self.frames += len(mic)
                self._maybe_cut()

    def _maybe_cut(self) -> None:
        seconds = self.mic.segment_seconds
        if seconds < self.segment_seconds:
            return
        quiet = self._recent and max(self._recent) < self.silence_db
        if quiet or seconds >= self.segment_seconds * 1.5:
            self._cut()

    def _cut(self) -> None:
        mic_info = self.mic.cut()
        sys_info = self.sys.cut() if self.sys is not None else None
        if mic_info is not None:
            try:
                self.on_segment(mic_info, sys_info)
            except Exception as exc:
                print(f"Warning: segment handler failed: {exc}")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        with audio.PORTAUDIO_LOCK:
            for stream in (self._stream, self._sys_stream):
                if stream is not None:
                    try:
                        stream.stop()
                        stream.close()
                    finally:
                        audio._stream_closed()
            self._stream = self._sys_stream = None
        with self._lock:
            self._cut()


# ─── Device selection ────────────────────────────────────────────────────────────


class DeviceBroker:
    """Builds (and caches) the capture device, falling back gracefully:
    tap + aggregate → a named second input device → microphone only."""

    def __init__(self):
        self._tap = None
        self._aggregate = None
        self._mic_uid: str | None = None
        self.last_error: str | None = None

    def open(self, settings: Settings) -> CaptureDevice:
        import sounddevice

        mic_rate = audio.device_samplerate(fallback=settings.sample_rate)
        mic_channels = 1
        try:
            default = sounddevice.query_devices(kind="input")
            mic_channels = max(1, min(2, int(default.get("max_input_channels", 1))))
        except Exception:
            pass

        if settings.meeting_system_audio:
            device = self._open_tap(settings, mic_channels)
            if device is not None:
                return device
            if settings.meeting_system_device:
                device = self._open_named(settings, mic_rate, mic_channels)
                if device is not None:
                    return device

        return CaptureDevice(None, mic_rate, mic_channels, mic_channels, False, "microphone only")

    def _open_tap(self, settings: Settings, mic_channels: int) -> CaptureDevice | None:
        from whisperlocal import systemaudio as sa

        ok, why = sa.is_available()
        if not ok:
            self.last_error = why
            print(f"Warning: system audio unavailable ({why}) — recording microphone only")
            return None
        try:
            mic_uid = sa.default_input_uid()
            if not mic_uid:
                raise sa.CoreAudioError("default input", 0)
            if self._aggregate is not None and mic_uid != self._mic_uid:
                self.close()
            if self._aggregate is None:
                exclude = []
                self._tap = sa.ProcessTap(exclude_pids=exclude)
                self._tap.create()
                self._aggregate = sa.AggregateDevice(mic_uid, self._tap)
                self._aggregate.create()
                self._mic_uid = mic_uid
                audio.reinitialize_portaudio()
            index = sa.find_portaudio_index(self._aggregate.name)
            if index is None:
                raise sa.CoreAudioError("aggregate device not visible to PortAudio", 0)
            import sounddevice

            dev = sounddevice.query_devices(index)
            channels = int(dev["max_input_channels"])
            rate = int(dev["default_samplerate"])
            if channels <= mic_channels:
                raise sa.CoreAudioError("aggregate device has no tap channels", 0)
            self.last_error = None
            label = f"microphone + system audio (tap {self._tap.tap_id}, {channels} ch @ {rate} Hz)"
            print(f"System audio tap created ({label})")
            return CaptureDevice(index, rate, channels, mic_channels, True, label)
        except sa.CoreAudioError as exc:
            self.last_error = str(exc)
            print(f"Warning: system audio unavailable ({exc.code}) — recording microphone only")
            self.close()
            return None
        except Exception as exc:
            self.last_error = str(exc)
            print(f"Warning: system audio unavailable ({exc}) — recording microphone only")
            self.close()
            return None

    def _open_named(self, settings: Settings, mic_rate: int, mic_channels: int) -> CaptureDevice | None:
        from whisperlocal import systemaudio as sa

        index = sa.find_portaudio_index(settings.meeting_system_device)
        if index is None:
            print(f"Warning: input device {settings.meeting_system_device!r} not found")
            return None
        import sounddevice

        dev = sounddevice.query_devices(index)
        rate = int(dev["default_samplerate"])
        if rate != mic_rate:
            print(
                f"Warning: {settings.meeting_system_device!r} runs at {rate} Hz but the "
                f"microphone at {mic_rate} Hz — system audio skipped"
            )
            return None
        label = f"microphone + {settings.meeting_system_device}"
        return CaptureDevice(
            None, mic_rate, mic_channels, mic_channels, True, label,
            system_index=index, system_channels=max(1, min(2, int(dev["max_input_channels"]))),
        )

    def close(self) -> None:
        if self._aggregate is not None:
            self._aggregate.destroy()
            self._aggregate = None
        if self._tap is not None:
            self._tap.destroy()
            self._tap = None
        self._mic_uid = None


# ─── The recorder ────────────────────────────────────────────────────────────────


class MeetingRecorder:
    """Start/stop meeting recordings and see them through to a transcript."""

    def __init__(self, settings: Settings, store: MeetingStore, transcriber, *, engine=None, sounds=None):
        self.settings = settings
        self.store = store
        self.transcriber = transcriber
        self.engine = engine
        self.sounds = sounds
        self.broker = DeviceBroker()
        self._listeners: list[Event] = []
        self._lock = threading.Lock()
        self._capture: MeetingCapture | None = None
        self._worker: LongformTranscriber | None = None
        self._meeting = None
        self._pending: list[tuple[SegmentInfo, SegmentInfo | None]] = []
        self._segments_total = 0
        self._segments_done = 0
        self._finalising = False
        self._device: CaptureDevice | None = None

    # ── events ───────────────────────────────────────────────────────────

    def on_event(self, fn: Event) -> None:
        self._listeners.append(fn)

    def _emit(self, name: str, **data) -> None:
        payload = {"meeting_id": self._meeting.id if self._meeting else None, **data}
        for fn in list(self._listeners):
            try:
                fn(name, payload)
            except Exception as exc:
                print(f"Warning: meeting event handler failed: {exc}")

    # ── settings ─────────────────────────────────────────────────────────

    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings

    # ── status ───────────────────────────────────────────────────────────

    @property
    def recording(self) -> bool:
        return self._capture is not None

    def status(self) -> dict:
        m = self._meeting
        cap = self._capture
        if m is None:
            return {"recording": False, "status": "idle"}
        return {
            "recording": cap is not None,
            "id": m.id,
            "title": m.title,
            "app": m.app,
            "started_at": m.started_at,
            "elapsed_s": round(cap.elapsed if cap else (m.duration_s or 0)),
            "segments_done": self._segments_done,
            "segments_total": self._segments_total,
            "status": "recording" if cap else ("transcribing" if self._finalising else m.status),
            "system_audio": bool(self._device and self._device.has_system),
            "device": self._device.label if self._device else None,
        }

    # ── start / stop ─────────────────────────────────────────────────────

    def _make_backend(self):
        s = self.settings
        transcriber = self.transcriber
        if s.meeting_backend == "local" and s.meeting_model_path != transcriber.model_path:
            from whisperlocal.transcription.local import Transcriber

            transcriber = Transcriber(dataclasses.replace(s, model=s.meeting_model or s.model))
        return make_backend(s, "meeting", transcriber=transcriber)

    def start(self, *, trigger: str = "manual", title: str | None = None,
              app: str | None = None, app_bundle_id: str | None = None) -> dict:
        with self._lock:
            if self._capture is not None:
                return self.status()
            if self._finalising:
                raise RuntimeError("the previous meeting is still being transcribed")

            s = self.settings
            backend = self._make_backend()
            device = self.broker.open(s)
            self._device = device

            meeting = self.store.create(
                app=app, app_bundle_id=app_bundle_id, trigger=trigger, backend=backend.name,
                model=backend.model, language=s.language, title=title,
            )
            self._meeting = meeting
            self._segments_total = self._segments_done = 0
            self._pending = []

            tracks = [{"name": "mic", "speaker": "You", "samplerate": device.samplerate}]
            if device.has_system:
                tracks.append({"name": "sys", "speaker": "Others", "samplerate": device.samplerate})
            self.store.update(meeting.id, tracks=tracks)

            segments_dir = self.store.path(meeting.id) / "audio" / "segments"
            self._worker = LongformTranscriber(
                backend,
                settings=s,
                on_result=self._on_result,
                should_wait=self._dictation_busy,
                language=s.whisper_language,
            )
            self._capture = MeetingCapture(
                device, segments_dir,
                segment_seconds=s.meeting_segment_seconds,
                silence_db=s.meeting_silence_db,
                on_segment=self._on_segment,
                on_no_audio=self._on_no_audio,
            )
            if self.sounds is not None:
                self.sounds.suppressed = True
            try:
                self._capture.start()
            except Exception:
                self._capture = None
                if self.sounds is not None:
                    self.sounds.suppressed = False
                self.store.set_status(meeting.id, "failed", error="could not open the microphone")
                self._meeting = None
                raise

        print(f"Meeting {meeting.id} started ({device.label})")
        self._emit("started", title=meeting.title, app=app, device=device.label)
        return self.status()

    def _on_no_audio(self) -> None:
        """The tap aggregate is not running (almost always: macOS has not
        allowed system audio capture). Carry on with the microphone alone
        rather than record silence for an hour."""
        from whisperlocal import systemaudio

        def _swap():
            with self._lock:
                dead = self._capture
                if dead is None or not dead.dead or self._meeting is None:
                    return
                dead.stop()
                self.broker.close()
                systemaudio.last_denial = "no audio delivered (permission not granted?)"
                s = self.settings
                device = CaptureDevice(None, audio.device_samplerate(fallback=s.sample_rate), 1, 1, False,
                                       "microphone only (system audio unavailable)")
                self._device = device
                self.store.update(self._meeting.id, tracks=[
                    {"name": "mic", "speaker": "You", "samplerate": device.samplerate}
                ])
                fresh = MeetingCapture(
                    device, dead.mic.directory,
                    segment_seconds=s.meeting_segment_seconds,
                    silence_db=s.meeting_silence_db,
                    on_segment=self._on_segment,
                )
                fresh.start()
                self._capture = fresh
            print("Warning: system audio unavailable — continuing with the microphone only")
            self._emit("system_audio_unavailable", device=device.label)

        threading.Thread(target=_swap, daemon=True).start()

    def _dictation_busy(self) -> bool:
        engine = self.engine
        return bool(engine and engine.state in (engine.RECORDING, engine.TRANSCRIBING))

    def _on_segment(self, mic: SegmentInfo, sys_info: SegmentInfo | None) -> None:
        meeting = self._meeting
        if meeting is None:
            return
        print(f"   Segment {mic.index} cut at {mic.end_s:.1f}s")
        self.store.append_segment(meeting.id, {
            "index": mic.index, "start_s": round(mic.start_s, 3), "end_s": round(mic.end_s, 3),
            "mic_path": str(mic.path), "sys_path": str(sys_info.path) if sys_info else None,
        })
        jobs = [mic] + ([sys_info] if sys_info else [])
        self._segments_total += len(jobs)
        if self.settings.meeting_transcribe_live:
            for job in jobs:
                self._worker.submit(job.index, job.track, job.path, job.start_s)
        else:
            self._pending.append((mic, sys_info))

    def _on_result(self, index: int, track: str, segments: list[Segment]) -> None:
        meeting = self._meeting
        if meeting is None:
            return
        self._segments_done += 1
        words = sum(len(seg.text.split()) for seg in segments)
        self.store.append_chunk(meeting.id, {
            "index": index, "track": track,
            "segments": [dataclasses.asdict(seg) for seg in segments],
        })
        print(f"   Segment {index}/{track} transcribed: {words} words")
        self._emit("segment_transcribed", index=index, track=track, words=words,
                   done=self._segments_done, total=self._segments_total)

    def stop(self, *, wait: bool = False) -> dict:
        with self._lock:
            capture = self._capture
            if capture is None:
                return self.status()
            self._capture = None
            capture.stop()
            if self.sounds is not None:
                self.sounds.suppressed = False
            meeting = self._meeting
            duration = capture.elapsed
            ended = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            self.store.update(meeting.id, ended_at=ended, duration_s=round(duration, 1))
            self.store.set_status(meeting.id, "transcribing")
            self._finalising = True

        print(f"Meeting {meeting.id} stopped after {duration / 60:.1f} min — transcribing")
        self._emit("stopped", duration_s=duration)

        if duration < self.settings.meeting_min_seconds and meeting.trigger != "manual":
            print(f"   (Shorter than meeting_min_seconds={self.settings.meeting_min_seconds}s — discarded)")
            self._worker.close()
            self.store.delete(meeting.id)
            self._meeting = None
            self._finalising = False
            self._emit("discarded")
            return {"recording": False, "status": "discarded"}

        thread = threading.Thread(target=self._finalize, args=(meeting, capture), daemon=True)
        thread.start()
        if wait:
            thread.join()
        return self.status()

    def _finalize(self, meeting, capture: MeetingCapture) -> None:
        try:
            if not self.settings.meeting_transcribe_live:
                for mic, sys_info in self._pending:
                    self._worker.submit(mic.index, mic.track, mic.path, mic.start_s)
                    if sys_info:
                        self._worker.submit(sys_info.index, sys_info.track, sys_info.path, sys_info.start_s)
                self._pending = []
            self._worker.drain()
            self._worker.close()

            mic_segs: list[Segment] = []
            sys_segs: list[Segment] = []
            for chunk in self.store.chunks(meeting.id):
                target = mic_segs if chunk.get("track") == "mic" else sys_segs
                for raw in chunk.get("segments", []):
                    words = tuple(
                        _word_from_dict(w) for w in raw.get("words", []) if isinstance(w, dict)
                    )
                    target.append(Segment(
                        float(raw["start"]), float(raw["end"]), str(raw["text"]),
                        words, raw.get("no_speech_prob"),
                    ))
            merged = merge_tracks(mic_segs, sys_segs, mic_rms=capture.mic_rms, sys_rms=capture.sys_rms)
            self.store.write_transcript(meeting.id, merged)

            self._finish_audio(meeting, capture)
            final = self.store.get(meeting.id)
            words = (final.stats or {}).get("words_total", 0) if final else 0
            print(f"Meeting {meeting.id} finalized: {(final.duration_s or 0) / 60:.1f} min, {words:,} words")
            self._emit("finalized", words=words)
        except Exception as exc:
            print(f"Error: meeting {meeting.id} could not be finalised: {exc}")
            try:
                self.store.set_status(meeting.id, "failed", error=str(exc))
            except Exception:
                pass
            self._emit("failed", error=str(exc))
        finally:
            self._finalising = False
            self._meeting = None
            self._worker = None

    def _finish_audio(self, meeting, capture: MeetingCapture) -> None:
        root = self.store.path(meeting.id) / "audio"
        segments_dir = root / "segments"
        if self.settings.meeting_keep_audio:
            fmt = self.settings.meeting_audio_format
            for writer, name in ((capture.mic, "mic"), (capture.sys, "system")):
                if writer is None:
                    continue
                files = sorted(segments_dir.glob(f"{writer.name}_*.wav"))
                if files:
                    concatenate_wavs(files, root / f"{name}.{fmt}", writer.samplerate, fmt)
        shutil.rmtree(segments_dir, ignore_errors=True)

    def shutdown(self) -> None:
        """Quit path: stop, wait for the transcript, release the tap."""
        if self._capture is not None:
            self.stop(wait=True)
        self.broker.close()


def _word_from_dict(d: dict):
    from whisperlocal.transcription.backends import Word

    return Word(str(d.get("text", "")), float(d.get("start", 0)), float(d.get("end", 0)))


def concatenate_wavs(files: list[Path], out: Path, samplerate: int, fmt: str) -> Path:
    """Join segment files into one FLAC/WAV, block by block."""
    import soundfile as sf

    out.parent.mkdir(parents=True, exist_ok=True)
    subtype = "PCM_16"
    with sf.SoundFile(str(out), "w", samplerate=samplerate, channels=1, subtype=subtype, format=fmt.upper()) as dst:
        for path in files:
            try:
                with sf.SoundFile(str(path)) as src:
                    for block in src.blocks(blocksize=65536, dtype="float32"):
                        dst.write(block)
            except Exception as exc:
                print(f"Warning: skipped {path.name} while joining audio: {exc}")
    return out


# ─── Re-transcribing from the terminal ───────────────────────────────────────────


def retranscribe(store: MeetingStore, meeting_id: str, settings: Settings,
                 *, backend_name: str | None = None) -> int:
    """Rebuild a meeting's transcript from its kept audio or leftover segments."""
    meeting = store.get(meeting_id)
    if meeting is None:
        print(f"No meeting {meeting_id!r}")
        return 1
    if backend_name:
        settings = dataclasses.replace(settings, meeting_backend=backend_name)

    root = store.path(meeting_id) / "audio"
    segments_dir = root / "segments"
    jobs: list[tuple[int, str, Path, float]] = []
    if segments_dir.exists() and any(segments_dir.glob("mic_*.wav")):
        log = {s["index"]: s for s in store.segments_log(meeting_id)}
        for path in sorted(segments_dir.glob("*.wav")):
            track, idx = path.stem.split("_")
            info = log.get(int(idx), {})
            jobs.append((int(idx), track, path, float(info.get("start_s", 0.0))))
    else:
        for name, track in (("mic", "mic"), ("system", "sys")):
            src = next((root / f"{name}.{ext}" for ext in ("flac", "wav") if (root / f"{name}.{ext}").exists()), None)
            if src is None:
                continue
            jobs.extend(split_audio(src, segments_dir, track, settings.meeting_segment_seconds))
    if not jobs:
        print("No audio kept for this meeting — nothing to transcribe.")
        return 1

    from whisperlocal.transcription.local import Transcriber

    transcriber = Transcriber(dataclasses.replace(settings, model=settings.meeting_model or settings.model))
    backend = make_backend(settings, "meeting", transcriber=transcriber)
    results: dict[str, list[Segment]] = {"mic": [], "sys": []}

    def _collect(index, track, segs):
        results.setdefault(track, []).extend(segs)
        print(f"   segment {index}/{track}: {sum(len(s.text.split()) for s in segs)} words")

    store.set_status(meeting_id, "transcribing")
    store.update(meeting_id, backend=backend.name, model=backend.model)
    worker = LongformTranscriber(backend, settings=settings, on_result=_collect, language=settings.whisper_language)
    for index, track, path, offset in sorted(jobs):
        worker.submit(index, track, path, offset)
    worker.drain()
    worker.close()
    merged = merge_tracks(results.get("mic", []), results.get("sys", []))
    store.write_transcript(meeting_id, merged)
    if not (root / "mic.flac").exists() and not (root / "mic.wav").exists():
        # Leftover segments from an interrupted meeting: join them now.
        for track, name in (("mic", "mic"), ("sys", "system")):
            files = sorted(segments_dir.glob(f"{track}_*.wav"))
            if files:
                import soundfile as sf

                rate = sf.info(str(files[0])).samplerate
                concatenate_wavs(files, root / f"{name}.{settings.meeting_audio_format}", rate, settings.meeting_audio_format)
    shutil.rmtree(segments_dir, ignore_errors=True)
    final = store.get(meeting_id)
    print(f"Done: {(final.stats or {}).get('words_total', 0):,} words → {store.path(meeting_id) / 'transcript.md'}")
    return 0


def split_audio(src: Path, out_dir: Path, track: str, segment_seconds: int) -> list[tuple[int, str, Path, float]]:
    """Cut a kept audio file back into segments for re-transcription."""
    import soundfile as sf

    out_dir.mkdir(parents=True, exist_ok=True)
    jobs = []
    with sf.SoundFile(str(src)) as f:
        rate = f.samplerate
        per = segment_seconds * rate
        index = 0
        while True:
            block = f.read(per, dtype="float32")
            if len(block) == 0:
                break
            path = out_dir / f"{track}_{index:04d}.wav"
            sf.write(str(path), block, rate, subtype="PCM_16")
            jobs.append((index, track, path, index * segment_seconds))
            index += 1
    return jobs
