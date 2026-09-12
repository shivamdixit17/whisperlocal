"""
Microphone capture.

Moved out of app.py. Two rules learned the hard way live here:

  * Audio is drained from a thread we own, never a PortAudio callback. Handing
    a Python callback to PortAudio runs the interpreter on CoreAudio's realtime
    IO thread and crashes there.
  * Recording runs at the input device's native rate. Asking PortAudio for a
    rate the hardware does not run at pushes it through a rate-adapting path
    that segfaults on the same thread.

PORTAUDIO_LOCK guards PortAudio's global state. Meeting capture creates an
aggregate device at runtime, and PortAudio only enumerates devices when it
initialises, so that path has to re-initialise the library while nothing else
has a stream open.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import sounddevice
import soundfile as sf

from whisperlocal.config import Settings

# Re-entrant so a caller already holding it can open a stream.
PORTAUDIO_LOCK = threading.RLock()
_open_streams = 0


def open_stream_count() -> int:
    return _open_streams


def _stream_opened() -> None:
    global _open_streams
    with PORTAUDIO_LOCK:
        _open_streams += 1


def _stream_closed() -> None:
    global _open_streams
    with PORTAUDIO_LOCK:
        _open_streams = max(0, _open_streams - 1)


def device_samplerate(device=None, fallback: int = 16000) -> int:
    """Native sample rate of an input device (the default one if None)."""
    try:
        info = sounddevice.query_devices(device=device, kind="input")
        return int(info["default_samplerate"])
    except Exception:
        return fallback


def reinitialize_portaudio(timeout: float = 10.0) -> bool:
    """
    Make PortAudio re-scan the device list.

    PortAudio builds its device table once, at initialisation, so a device
    created afterwards (the meeting aggregate) is invisible until the library
    is torn down and brought back. That is only safe when no stream is open, so
    this waits for push-to-talk to finish (its streams last seconds) and gives
    up rather than yanking a live stream away.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        with PORTAUDIO_LOCK:
            if _open_streams == 0:
                try:
                    sounddevice._terminate()
                    sounddevice._initialize()
                    return True
                except Exception as exc:
                    print(f"Warning: could not re-initialise PortAudio: {exc}")
                    return False
        time.sleep(0.1)
    print("Warning: PortAudio busy — device list not refreshed")
    return False


class AudioRecorder:
    """Records audio from the default microphone."""

    BLOCKSIZE = 1024

    def __init__(self, settings: Settings):
        self.settings = settings
        self._frames: list = []
        self._stream = None
        self._recording = False
        self._start_time: float | None = None
        self._thread: threading.Thread | None = None
        self._samplerate = settings.sample_rate
        # Guards _frames. The drain thread appends to it while a live
        # dictation worker reads and trims it.
        self._lock = threading.Lock()

    def _device_samplerate(self) -> int:
        """Native sample rate of the default input device. See module docstring."""
        return device_samplerate(fallback=self.settings.sample_rate)

    def start(self) -> None:
        """Open the microphone and start collecting frames."""
        with self._lock:
            self._frames = []
        self._recording = True
        self._start_time = time.time()
        self._samplerate = self._device_samplerate()

        with PORTAUDIO_LOCK:
            self._stream = sounddevice.InputStream(
                samplerate=self._samplerate,
                channels=self.settings.channels,
                dtype="float32",
                blocksize=self.BLOCKSIZE,
            )
            self._stream.start()
            _stream_opened()

        # Drain the stream from a thread we own. Handing a Python callback to
        # PortAudio runs the interpreter on CoreAudio's realtime IO thread and
        # crashes there (EXC_BAD_ACCESS in _PyEval_EvalFrameDefault, via cffi's
        # closure trampoline).
        def _drain():
            while self._recording:
                try:
                    data, overflowed = self._stream.read(self.BLOCKSIZE)
                except Exception as exc:
                    print(f"Warning: audio read error: {exc}")
                    break
                if overflowed:
                    print("Warning: audio buffer overflow")
                with self._lock:
                    self._frames.append(data.copy())

        self._thread = threading.Thread(target=_drain, daemon=True)
        self._thread.start()
        print("Recording...")

    def stop(self) -> float:
        """Close the microphone and return how long we recorded for."""
        self._recording = False

        # Join before closing: the drain thread must not be inside read() when
        # the stream goes away.
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

        if self._stream:
            with PORTAUDIO_LOCK:
                try:
                    self._stream.stop()
                    self._stream.close()
                finally:
                    self._stream = None
                    _stream_closed()

        duration = time.time() - self._start_time if self._start_time else 0.0
        self._start_time = None
        print(f"Recording stopped ({duration:.1f}s)")
        return duration

    def snapshot(self):
        """Everything captured so far, without stopping the recording.

        Live dictation re-transcribes the buffer while it is still filling.
        """
        with self._lock:
            frames = list(self._frames)
        if not frames:
            return np.zeros(0, dtype="float32"), self._samplerate
        return np.concatenate(frames, axis=0), self._samplerate

    def drop_leading(self, seconds: float) -> float:
        """Discard `seconds` of audio from the front of the buffer.

        Keeps live sessions inside whisper's 30s attention window, beyond which
        inference cost triples. Frames are fixed-size so this rounds down to a
        whole number of them, and returns how much was actually dropped so the
        caller can keep its own timeline in sync.
        """
        if seconds <= 0:
            return 0.0

        with self._lock:
            if not self._frames:
                return 0.0
            frames_to_drop = int(seconds * self._samplerate) // self.BLOCKSIZE
            frames_to_drop = min(frames_to_drop, len(self._frames))
            if frames_to_drop <= 0:
                return 0.0
            del self._frames[:frames_to_drop]

        return frames_to_drop * self.BLOCKSIZE / self._samplerate

    def save(self, filepath) -> bool:
        """Write the captured audio to a WAV file."""
        with self._lock:
            frames = list(self._frames)
        if not frames:
            return False

        audio_data = np.concatenate(frames, axis=0)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(filepath), audio_data, self._samplerate)
        return True

    @property
    def is_recording(self) -> bool:
        return self._recording

