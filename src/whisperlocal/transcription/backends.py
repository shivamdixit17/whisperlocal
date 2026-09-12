"""
Transcription backends behind one small interface.

Two implementations:

  * LocalMLXBackend wraps transcription.local.Transcriber, the on-device
    mlx-whisper engine.
  * OpenAICompatibleBackend posts audio to any server speaking the OpenAI
    `/audio/transcriptions` protocol (OpenAI itself, Groq, a self-hosted
    faster-whisper server, ...).

Both return a TranscriptResult: text, timed segments, and enough provenance
(backend, model, elapsed time) for the meeting record. This module imports
none of the macOS or ML stack, so it runs on Linux CI; the local backend only
touches MLX through the Transcriber it is handed.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Literal, Protocol, runtime_checkable

from whisperlocal import __version__

MAX_UPLOAD_BYTES = 24 * 1024 * 1024
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
BACKOFF_SECONDS = (1.0, 3.0, 9.0)

MISSING_KEY_MESSAGE = "API key missing — run: whisperlocal api-key set"


# ─── Result types ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str
    words: tuple[Word, ...] = ()
    no_speech_prob: float | None = None

    def shifted(self, offset_s: float) -> "Segment":
        return replace(
            self,
            start=self.start + offset_s,
            end=self.end + offset_s,
            words=tuple(Word(w.text, w.start + offset_s, w.end + offset_s) for w in self.words),
        )


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    segments: tuple[Segment, ...]
    language: str | None
    backend: str
    model: str
    elapsed_ms: float

    def shifted(self, offset_s: float) -> "TranscriptResult":
        """The same result with every timestamp moved by `offset_s` seconds."""
        if not offset_s:
            return self
        return replace(self, segments=tuple(s.shifted(offset_s) for s in self.segments))


class BackendError(RuntimeError):
    """Transcription failed in a way the caller should report, not retry."""


@runtime_checkable
class TranscriptionBackend(Protocol):
    name: str
    model: str

    def transcribe_file(
        self, path: Path, *, language: str | None, timestamps: bool
    ) -> TranscriptResult: ...

    def warm(self) -> None: ...

    def check(self) -> tuple[bool, str]: ...


# ─── Shared parsing ──────────────────────────────────────────────────────────────


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def segments_from_dicts(raw_segments: Any) -> tuple[Segment, ...]:
    """Map whisper-style segment dicts (mlx_whisper or verbose_json) to Segments."""
    out: list[Segment] = []
    for seg in raw_segments or ():
        if not isinstance(seg, dict):
            continue
        words = tuple(
            Word(
                text=str(w.get("word", w.get("text", ""))),
                start=_float(w.get("start")),
                end=_float(w.get("end")),
            )
            for w in (seg.get("words") or ())
            if isinstance(w, dict)
        )
        nsp = seg.get("no_speech_prob")
        out.append(
            Segment(
                start=_float(seg.get("start")),
                end=_float(seg.get("end")),
                text=str(seg.get("text", "")),
                words=words,
                no_speech_prob=None if nsp is None else _float(nsp),
            )
        )
    return tuple(out)


def result_from_dict(
    raw: dict, *, backend: str, model: str, elapsed_ms: float, language: str | None = None
) -> TranscriptResult:
    """Build a TranscriptResult from a whisper-shaped dict.

    Without "segments" (plain `json` responses, or a text-only result) the
    whole text becomes one Segment at 0..0.
    """
    text = str(raw.get("text", "") or "").strip()
    segments = segments_from_dicts(raw.get("segments"))
    if not segments and text:
        segments = (Segment(0.0, 0.0, text),)
    if not text and segments:
        text = " ".join(s.text.strip() for s in segments if s.text.strip())
    lang = raw.get("language")
    return TranscriptResult(
        text=text,
        segments=segments,
        language=str(lang) if lang else language,
        backend=backend,
        model=model,
        elapsed_ms=elapsed_ms,
    )


# ─── Local (MLX) ─────────────────────────────────────────────────────────────────


class LocalMLXBackend:
    """The on-device engine.

    Duck-typed over transcription.local.Transcriber: it needs `transcribe(path)`,
    `transcribe_segments(path, language=)`, `warm()` and `model_path`. The
    backend always uses the transcriber's own model; for meetings on a
    different model (settings.meeting_model_path) construct a second
    Transcriber and wrap that — the MLX cache holds one model at a time, so
    swapping per call would thrash it.
    """

    name = "local"

    def __init__(self, transcriber, *, model: str | None = None):
        self.transcriber = transcriber
        self.model = model or getattr(transcriber, "model_path", "") or ""

    def transcribe_file(
        self, path: Path, *, language: str | None = None, timestamps: bool = False
    ) -> TranscriptResult:
        started = time.monotonic()
        if timestamps:
            raw = self.transcriber.transcribe_segments(path, language=language)
            if raw is None:
                raise BackendError("transcription failed")
            elapsed = (time.monotonic() - started) * 1000
            return result_from_dict(
                raw, backend=self.name, model=self.model, elapsed_ms=elapsed, language=language
            )

        text = self.transcriber.transcribe(path)
        if text is None:
            raise BackendError("transcription failed")
        elapsed = (time.monotonic() - started) * 1000
        text = text.strip()
        return TranscriptResult(
            text=text,
            segments=(Segment(0.0, 0.0, text),) if text else (),
            language=language,
            backend=self.name,
            model=self.model,
            elapsed_ms=elapsed,
        )

    def warm(self) -> None:
        warm = getattr(self.transcriber, "warm", None)
        if warm:
            warm()

    def check(self) -> tuple[bool, str]:
        return True, f"local · {self.model}"


# ─── OpenAI-compatible HTTP ──────────────────────────────────────────────────────


def encode_multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    """multipart/form-data body and its Content-Type header.

    `files` maps field name -> (filename, data, mimetype).
    """
    boundary = "----WhisperLocal" + uuid.uuid4().hex
    crlf = b"\r\n"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(b"--" + boundary.encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        parts.append(b"")
        parts.append(str(value).encode("utf-8"))
    for name, (filename, data, mimetype) in files.items():
        parts.append(b"--" + boundary.encode())
        parts.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'.encode()
        )
        parts.append(f"Content-Type: {mimetype}".encode())
        parts.append(b"")
        parts.append(data)
    parts.append(b"--" + boundary.encode() + b"--")
    parts.append(b"")
    return crlf.join(parts), f"multipart/form-data; boundary={boundary}"


class OpenAICompatibleBackend:
    """POST audio to `{base_url}/audio/transcriptions`.

    The audio is first re-encoded to 16 kHz mono AAC with ffmpeg: a minute of
    FLAC or WAV is many megabytes, a minute of 48 kbit/s AAC is 360 KB, and the
    API caps uploads at 25 MB. Transient failures (429, 5xx, network) are
    retried with backoff; anything that will not fix itself (401, 400) is not.
    """

    name = "api"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        *,
        timeout_s: int = 120,
        retries: int = 3,
        opener: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        ffmpeg: str = "ffmpeg",
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = (api_key or "").strip()
        self.timeout_s = timeout_s
        self.retries = max(1, retries)
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep
        self.ffmpeg = ffmpeg

    # ── public ───────────────────────────────────────────────────────────────

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/audio/transcriptions"

    def warm(self) -> None:
        return None

    def check(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, MISSING_KEY_MESSAGE
        return True, f"{self.base_url} · {self.model}"

    def transcribe_file(
        self, path: Path, *, language: str | None = None, timestamps: bool = False
    ) -> TranscriptResult:
        if not self.api_key:
            raise BackendError(MISSING_KEY_MESSAGE)
        path = Path(path)
        started = time.monotonic()
        encoded = self._encode(path)
        try:
            size = encoded.stat().st_size
            if size > MAX_UPLOAD_BYTES:
                raise BackendError(
                    f"audio is {size / 2**20:.1f} MB after encoding; the API accepts at most "
                    f"{MAX_UPLOAD_BYTES / 2**20:.0f} MB"
                )
            data = encoded.read_bytes()
        finally:
            try:
                encoded.unlink()
            except OSError:
                pass

        fields: dict[str, str] = {
            "model": self.model,
            "response_format": "verbose_json" if timestamps else "json",
        }
        if language:
            fields["language"] = language
        if timestamps:
            fields["timestamp_granularities[]"] = "segment"
        body, content_type = encode_multipart(
            fields, {"file": ("audio.m4a", data, "audio/mp4")}
        )
        raw = self._post(body, content_type)
        elapsed = (time.monotonic() - started) * 1000
        return result_from_dict(
            raw, backend=self.name, model=self.model, elapsed_ms=elapsed, language=language
        )

    # ── internals ────────────────────────────────────────────────────────────

    def _encode(self, path: Path) -> Path:
        """16 kHz mono AAC in a temp file next to the input. Caller deletes it."""
        if not path.exists():
            raise BackendError(f"audio file not found: {path}")
        fd, out = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".m4a", dir=str(path.parent))
        os.close(fd)
        cmd = [
            self.ffmpeg,
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "aac",
            "-b:a",
            "48k",
            out,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except OSError as exc:
            self._unlink(out)
            raise BackendError(f"could not run {self.ffmpeg}: {exc} (is ffmpeg installed?)") from exc
        if proc.returncode != 0:
            self._unlink(out)
            err = (proc.stderr or "").strip().splitlines()
            raise BackendError(f"ffmpeg failed ({proc.returncode}): {err[-1] if err else 'no output'}")
        return Path(out)

    @staticmethod
    def _unlink(path: str | Path) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    def _post(self, body: bytes, content_type: str) -> dict:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": f"WhisperLocal/{__version__}",
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "Accept": "application/json",
        }
        last_error: str = "unknown error"
        for attempt in range(self.retries):
            request = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
            try:
                with self._opener(request, timeout=self.timeout_s) as response:
                    payload = response.read()
                return self._parse(payload)
            except urllib.error.HTTPError as exc:
                detail = self._error_detail(exc)
                if exc.code in (401, 403):
                    raise BackendError(f"API key rejected ({exc.code}){detail}") from exc
                if exc.code not in RETRY_STATUSES:
                    raise BackendError(f"API request failed ({exc.code}){detail}") from exc
                last_error = f"HTTP {exc.code}{detail}"
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
                reason = getattr(exc, "reason", None) or exc
                last_error = f"{type(exc).__name__}: {reason}"
            if attempt + 1 < self.retries:
                self._sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
        raise BackendError(f"API request failed after {self.retries} attempts: {last_error}")

    @staticmethod
    def _error_detail(exc: urllib.error.HTTPError) -> str:
        try:
            text = exc.read().decode("utf-8", "replace").strip()
        except Exception:
            return ""
        if not text:
            return ""
        try:
            parsed = json.loads(text)
            message = parsed.get("error", {}).get("message") if isinstance(parsed, dict) else None
            if message:
                text = str(message)
        except (ValueError, AttributeError):
            pass
        return f": {text[:200]}"

    @staticmethod
    def _parse(payload: bytes) -> dict:
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BackendError(f"API returned something that is not JSON: {payload[:120]!r}") from exc
        if not isinstance(parsed, dict):
            raise BackendError(f"API returned an unexpected shape: {type(parsed).__name__}")
        return parsed


# ─── Factory ─────────────────────────────────────────────────────────────────────


def make_backend(
    settings,
    purpose: Literal["dictation", "meeting"],
    *,
    transcriber=None,
    api_key: str | None = None,
) -> TranscriptionBackend:
    """The backend `settings` asks for, for dictation or for meetings.

    "local" needs a Transcriber. "api" reads the key from the keychain unless
    one is passed; an empty key is fine here and fails at the first
    transcribe_file call, before any network traffic, so the app can start
    and tell the user what is missing.
    """
    if purpose == "dictation":
        kind = settings.dictation_backend
    elif purpose == "meeting":
        kind = settings.meeting_backend
    else:
        raise ValueError(f"purpose must be 'dictation' or 'meeting', not {purpose!r}")

    if kind == "local":
        if transcriber is None:
            raise ValueError("the local backend needs a Transcriber")
        return LocalMLXBackend(transcriber)
    if kind == "api":
        if api_key is None:
            from whisperlocal import keychain

            api_key = keychain.get_api_key()
        return OpenAICompatibleBackend(
            settings.api_base_url,
            settings.api_model,
            api_key or "",
            timeout_s=settings.api_timeout_seconds,
        )
    raise ValueError(f"unknown backend {kind!r}; choose 'local' or 'api'")


__all__ = [
    "BackendError",
    "LocalMLXBackend",
    "MAX_UPLOAD_BYTES",
    "MISSING_KEY_MESSAGE",
    "OpenAICompatibleBackend",
    "Segment",
    "TranscriptResult",
    "TranscriptionBackend",
    "Word",
    "encode_multipart",
    "make_backend",
    "result_from_dict",
    "segments_from_dicts",
]
