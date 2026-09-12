"""File-level pieces of the meeting recorder. Needs soundfile + numpy, so it
skips on CI machines without them (the logic itself has no macOS parts)."""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")
sf = pytest.importorskip("soundfile")
pytest.importorskip("sounddevice")

from whisperlocal import meetingrecorder as mr  # noqa: E402


def test_track_writer_cuts_numbered_segments(tmp_path):
    w = mr.TrackWriter(tmp_path, "mic", 1000)
    w.write(np.zeros(500, dtype="float32"))
    w.write(np.ones(500, dtype="float32") * 0.5)
    assert w.segment_seconds == 1.0
    info = w.cut()
    assert info.index == 0 and info.start_s == 0 and info.end_s == 1.0
    assert info.path.name == "mic_0000.wav" and info.path.exists()
    w.write(np.zeros(250, dtype="float32"))
    info2 = w.close()
    assert info2.index == 1 and info2.start_s == 1.0 and info2.end_s == 1.25
    assert w.cut() is None
    data, rate = sf.read(str(info.path))
    assert rate == 1000 and len(data) == 1000


def test_concatenate_wavs_to_flac(tmp_path):
    files = []
    for i in range(3):
        p = tmp_path / f"mic_{i:04d}.wav"
        sf.write(str(p), np.full(800, 0.1 * (i + 1), dtype="float32"), 800, subtype="PCM_16")
        files.append(p)
    out = mr.concatenate_wavs(files, tmp_path / "out" / "mic.flac", 800, "flac")
    info = sf.info(str(out))
    assert info.format == "FLAC" and info.frames == 2400 and info.samplerate == 800


def test_split_audio_round_trip(tmp_path):
    src = tmp_path / "mic.wav"
    sf.write(str(src), np.zeros(800 * 7, dtype="float32"), 800, subtype="PCM_16")
    jobs = mr.split_audio(src, tmp_path / "segments", "mic", 3)
    assert [j[0] for j in jobs] == [0, 1, 2]
    assert [j[3] for j in jobs] == [0, 3, 6]
    assert sf.info(str(jobs[-1][2])).frames == 800


def test_rms_db():
    assert mr._rms_db(np.zeros(10, dtype="float32")) <= -100
    assert -1 < mr._rms_db(np.ones(10, dtype="float32")) <= 0.01
