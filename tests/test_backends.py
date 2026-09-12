"""backends: the HTTP backend against a fake opener, the local one against a fake Transcriber."""

from __future__ import annotations

import io
import json
import re
import subprocess
import urllib.error
from dataclasses import dataclass
from pathlib import Path

import pytest

from whisperlocal.transcription import backends
from whisperlocal.transcription.backends import (
    BackendError,
    LocalMLXBackend,
    OpenAICompatibleBackend,
    Segment,
    TranscriptResult,
    Word,
    encode_multipart,
    make_backend,
)

# ─── Fakes ───────────────────────────────────────────────────────────────────────


class FakeFFmpeg:
    """Stands in for subprocess.run: writes `output` to the last argument."""

    def __init__(self, output: bytes = b"AAC" * 100, returncode: int = 0):
        self.output = output
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if self.returncode == 0:
            Path(cmd[-1]).write_bytes(self.output)
        return subprocess.CompletedProcess(cmd, self.returncode, "", "ffmpeg: bad input")


class FakeResponse:
    def __init__(self, payload: dict | bytes):
        self._data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code: int, body: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body.encode()))


class FakeOpener:
    """Returns/raises `outcomes` in order; records every request."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []
        self.timeouts = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return FakeResponse(outcome)


@pytest.fixture
def wav(tmp_path) -> Path:
    p = tmp_path / "chunk.wav"
    p.write_bytes(b"RIFF" + b"\0" * 64)
    return p


@pytest.fixture
def ffmpeg(monkeypatch):
    fake = FakeFFmpeg()
    monkeypatch.setattr(backends.subprocess, "run", fake)
    return fake


def backend(opener, **kw):
    sleeps: list[float] = []
    b = OpenAICompatibleBackend(
        kw.pop("base_url", "https://api.example.com/v1/"),
        kw.pop("model", "whisper-1"),
        kw.pop("api_key", "sk-test"),
        opener=opener,
        sleep=sleeps.append,
        **kw,
    )
    return b, sleeps


# ─── multipart ───────────────────────────────────────────────────────────────────


def test_multipart_body_has_fields_file_and_boundary():
    body, content_type = encode_multipart(
        {"model": "whisper-1", "language": "en"}, {"file": ("audio.m4a", b"\x00\x01BIN", "audio/mp4")}
    )
    m = re.match(r"multipart/form-data; boundary=(.+)$", content_type)
    assert m
    boundary = m.group(1).encode()
    assert body.startswith(b"--" + boundary + b"\r\n")
    assert body.rstrip().endswith(b"--" + boundary + b"--")
    assert b'name="model"\r\n\r\nwhisper-1\r\n' in body
    assert b'name="language"\r\n\r\nen\r\n' in body
    assert b'name="file"; filename="audio.m4a"\r\nContent-Type: audio/mp4\r\n\r\n\x00\x01BIN\r\n' in body
    assert body.count(b"--" + boundary) == 4  # 3 parts + closing


def test_request_shape(wav, ffmpeg):
    opener = FakeOpener({"text": "hi"})
    b, _ = backend(opener, timeout_s=42)
    b.transcribe_file(wav, language="de", timestamps=True)

    req = opener.requests[0]
    assert req.full_url == "https://api.example.com/v1/audio/transcriptions"
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer sk-test"
    assert req.get_header("User-agent").startswith("WhisperLocal/")
    assert opener.timeouts == [42]
    body = req.data
    assert b'name="response_format"\r\n\r\nverbose_json' in body
    assert b'name="language"\r\n\r\nde' in body
    assert b'name="timestamp_granularities[]"\r\n\r\nsegment' in body
    assert b"AAC" * 100 in body

    cmd = ffmpeg.calls[0]
    assert cmd[0] == "ffmpeg"
    assert cmd[1:6] == ["-nostdin", "-y", "-loglevel", "error", "-i"]
    assert cmd[6] == str(wav)
    assert cmd[7:] [:-1] == ["-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "48k"]
    assert cmd[-1].endswith(".m4a")
    assert not Path(cmd[-1]).exists(), "temp m4a must be deleted"
    assert list(wav.parent.glob("*.m4a")) == []


def test_plain_json_without_timestamps(wav, ffmpeg):
    opener = FakeOpener({"text": "  Hello there.  "})
    b, _ = backend(opener)
    result = b.transcribe_file(wav, language=None, timestamps=False)
    assert isinstance(result, TranscriptResult)
    assert result.text == "Hello there."
    assert result.segments == (Segment(0.0, 0.0, "Hello there."),)
    assert result.backend == "api" and result.model == "whisper-1"
    assert result.language is None
    assert result.elapsed_ms >= 0
    body = opener.requests[0].data
    assert b'name="response_format"\r\n\r\njson\r\n' in body
    assert b"timestamp_granularities" not in body
    assert b'name="language"' not in body


def test_verbose_json_parsed(wav, ffmpeg):
    payload = {
        "text": "Hello world. Bye.",
        "language": "english",
        "segments": [
            {
                "start": 0.5,
                "end": 1.5,
                "text": " Hello world.",
                "no_speech_prob": 0.01,
                "words": [{"word": " Hello", "start": 0.5, "end": 0.9}, {"word": " world.", "start": 1.0, "end": 1.5}],
            },
            {"start": 2.0, "end": 2.5, "text": " Bye."},
        ],
    }
    b, _ = backend(FakeOpener(payload))
    result = b.transcribe_file(wav, language=None, timestamps=True)
    assert result.language == "english"
    assert len(result.segments) == 2
    first = result.segments[0]
    assert first.start == 0.5 and first.end == 1.5 and first.no_speech_prob == 0.01
    assert first.words == (Word(" Hello", 0.5, 0.9), Word(" world.", 1.0, 1.5))
    assert result.segments[1].words == () and result.segments[1].no_speech_prob is None


def test_retries_on_429_then_succeeds(wav, ffmpeg):
    opener = FakeOpener(http_error(429), http_error(503), {"text": "ok"})
    b, sleeps = backend(opener)
    assert b.transcribe_file(wav, language=None, timestamps=False).text == "ok"
    assert sleeps == [1, 3]
    assert len(opener.requests) == 3


def test_401_does_not_retry(wav, ffmpeg):
    opener = FakeOpener(http_error(401, '{"error": {"message": "bad key"}}'))
    b, sleeps = backend(opener)
    with pytest.raises(BackendError, match=r"API key rejected \(401\): bad key"):
        b.transcribe_file(wav, language=None, timestamps=False)
    assert sleeps == [] and len(opener.requests) == 1


def test_400_does_not_retry(wav, ffmpeg):
    opener = FakeOpener(http_error(400))
    b, sleeps = backend(opener)
    with pytest.raises(BackendError, match=r"\(400\)"):
        b.transcribe_file(wav, language=None, timestamps=False)
    assert len(opener.requests) == 1


def test_urlerror_exhausts_retries(wav, ffmpeg):
    opener = FakeOpener(
        urllib.error.URLError("dns"), TimeoutError("slow"), urllib.error.URLError("dns again")
    )
    b, sleeps = backend(opener)
    with pytest.raises(BackendError, match="after 3 attempts.*dns again"):
        b.transcribe_file(wav, language=None, timestamps=False)
    assert sleeps == [1, 3]
    assert len(opener.requests) == 3


def test_oversize_rejected_before_network(wav, monkeypatch):
    big = FakeFFmpeg(output=b"\0" * (backends.MAX_UPLOAD_BYTES + 1))
    monkeypatch.setattr(backends.subprocess, "run", big)
    opener = FakeOpener({"text": "never"})
    b, _ = backend(opener)
    with pytest.raises(BackendError, match="at most 24 MB"):
        b.transcribe_file(wav, language=None, timestamps=False)
    assert opener.requests == []
    assert list(wav.parent.glob("*.m4a")) == []


def test_missing_key_raises_before_anything(wav, ffmpeg):
    opener = FakeOpener({"text": "never"})
    b, _ = backend(opener, api_key="")
    with pytest.raises(BackendError, match="API key missing"):
        b.transcribe_file(wav, language=None, timestamps=False)
    assert opener.requests == [] and ffmpeg.calls == []
    assert b.check() == (False, backends.MISSING_KEY_MESSAGE)


def test_ffmpeg_failure(wav, monkeypatch):
    monkeypatch.setattr(backends.subprocess, "run", FakeFFmpeg(returncode=1))
    opener = FakeOpener({"text": "never"})
    b, _ = backend(opener)
    with pytest.raises(BackendError, match="ffmpeg failed"):
        b.transcribe_file(wav, language=None, timestamps=False)
    assert opener.requests == []
    assert list(wav.parent.glob("*.m4a")) == []


def test_check_and_warm():
    b, _ = backend(FakeOpener(), base_url="http://localhost:8000")
    assert b.warm() is None
    assert b.check() == (True, "http://localhost:8000 · whisper-1")
    assert b.endpoint == "http://localhost:8000/audio/transcriptions"


def test_bad_json_response(wav, ffmpeg):
    b, _ = backend(FakeOpener(b"<html>nope</html>"))
    with pytest.raises(BackendError, match="not JSON"):
        b.transcribe_file(wav, language=None, timestamps=False)


# ─── Local ───────────────────────────────────────────────────────────────────────


class FakeTranscriber:
    model_path = "mlx-community/whisper-small-mlx"

    def __init__(self, text="hello", raw=None):
        self._text = text
        self._raw = raw
        self.calls = []
        self.warmed = False

    def transcribe(self, path):
        self.calls.append(("transcribe", path))
        return self._text

    def transcribe_segments(self, path, *, language=None):
        self.calls.append(("segments", path, language))
        return self._raw

    def warm(self):
        self.warmed = True


def test_local_backend_maps_segments():
    raw = {
        "text": " one two ",
        "language": "en",
        "segments": [
            {"start": 0.0, "end": 1.0, "text": " one", "no_speech_prob": 0.2, "words": [{"word": " one", "start": 0.0, "end": 1.0}]},
            {"start": 1.0, "end": 2.0, "text": " two", "no_speech_prob": 0.1, "words": []},
        ],
    }
    t = FakeTranscriber(raw=raw)
    b = LocalMLXBackend(t)
    assert b.name == "local" and b.model == t.model_path
    result = b.transcribe_file(Path("a.wav"), language="en", timestamps=True)
    assert result.text == "one two"
    assert result.language == "en"
    assert result.backend == "local" and result.model == t.model_path
    assert result.segments[0] == Segment(0.0, 1.0, " one", (Word(" one", 0.0, 1.0),), 0.2)
    assert result.segments[1].words == ()
    assert t.calls == [("segments", Path("a.wav"), "en")]
    b.warm()
    assert t.warmed
    assert b.check()[0] is True


def test_local_backend_without_timestamps():
    t = FakeTranscriber(text="just text")
    result = LocalMLXBackend(t).transcribe_file(Path("a.wav"), language=None, timestamps=False)
    assert result.text == "just text"
    assert result.segments == (Segment(0.0, 0.0, "just text"),)
    assert t.calls == [("transcribe", Path("a.wav"))]


def test_local_backend_none_is_error():
    t = FakeTranscriber(text=None, raw=None)
    with pytest.raises(BackendError):
        LocalMLXBackend(t).transcribe_file(Path("a.wav"), language=None, timestamps=False)
    with pytest.raises(BackendError):
        LocalMLXBackend(t).transcribe_file(Path("a.wav"), language=None, timestamps=True)


# ─── shifted / factory ───────────────────────────────────────────────────────────


def test_shifted_offsets_everything():
    r = TranscriptResult(
        text="a b",
        segments=(Segment(1.0, 2.0, "a", (Word("a", 1.0, 1.5),)), Segment(2.0, 3.0, "b")),
        language=None,
        backend="x",
        model="y",
        elapsed_ms=1.0,
    )
    s = r.shifted(60.0)
    assert s.segments[0].start == 61.0 and s.segments[0].end == 62.0
    assert s.segments[0].words == (Word("a", 61.0, 61.5),)
    assert s.segments[1].start == 62.0
    assert s.text == "a b" and s.backend == "x"
    assert r.segments[0].start == 1.0  # original untouched
    assert r.shifted(0) is r


@dataclass
class FakeSettings:
    dictation_backend: str = "local"
    meeting_backend: str = "api"
    api_base_url: str = "https://api.openai.com/v1"
    api_model: str = "whisper-1"
    api_timeout_seconds: int = 77


def test_make_backend_selection(monkeypatch):
    t = FakeTranscriber()
    settings = FakeSettings()
    local = make_backend(settings, "dictation", transcriber=t)
    assert isinstance(local, LocalMLXBackend) and local.transcriber is t

    api = make_backend(settings, "meeting", api_key="sk-given")
    assert isinstance(api, OpenAICompatibleBackend)
    assert api.api_key == "sk-given" and api.timeout_s == 77
    assert api.base_url == "https://api.openai.com/v1"

    from whisperlocal import keychain

    monkeypatch.setattr(keychain, "get_api_key", lambda: "sk-keychain")
    assert make_backend(settings, "meeting").api_key == "sk-keychain"

    monkeypatch.setattr(keychain, "get_api_key", lambda: None)
    empty = make_backend(settings, "meeting")
    assert empty.api_key == "" and empty.check()[0] is False

    with pytest.raises(ValueError):
        make_backend(settings, "dictation")  # local without a transcriber
    with pytest.raises(ValueError):
        make_backend(FakeSettings(dictation_backend="cloud"), "dictation", transcriber=t)
