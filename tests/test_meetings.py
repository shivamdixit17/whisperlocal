"""meetings: the on-disk store, exercised in tmp_path."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from whisperlocal import meetings
from whisperlocal.meetings import Meeting, MeetingStore, new_meeting_id, slug
from whisperlocal.transcription.backends import Segment, Word
from whisperlocal.transcription.longform import SpeakerSegment

TZ = timezone(timedelta(hours=2))


@pytest.fixture
def store(tmp_path) -> MeetingStore:
    return MeetingStore(tmp_path / "meetings")


def create(store, *, app="Zoom", started_at=None, title=None, **kw):
    return store.create(
        app=app,
        app_bundle_id=kw.pop("app_bundle_id", "us.zoom.xos"),
        trigger=kw.pop("trigger", "manual"),
        backend=kw.pop("backend", "local"),
        model=kw.pop("model", "mlx-community/whisper-small-mlx"),
        language=kw.pop("language", "en"),
        title=title,
        started_at=started_at,
    )


SEGS = [
    SpeakerSegment(0.0, 10.0, "You", "mic", "Let us talk about the quarterly roadmap today"),
    SpeakerSegment(10.0, 20.0, "Others", "sys", "Sure, the budget review comes first though"),
    SpeakerSegment(20.0, 25.0, "You", "mic", "Fine by me"),
]


# ─── ids ─────────────────────────────────────────────────────────────────────────


def test_id_format_and_slug():
    dt = datetime(2026, 9, 12, 14, 5, 9, tzinfo=TZ)
    id = new_meeting_id(dt, "Microsoft Teams (work)")
    assert re.fullmatch(r"2026-09-12T14-05-09_microsoft-teams-work_[0-9a-f]{6}", id)
    assert meetings.is_meeting_id(id)
    assert new_meeting_id(dt, None).startswith("2026-09-12T14-05-09_meeting_")
    assert slug("  Zoom!! ") == "zoom"
    assert slug("") == "meeting"
    assert slug("Ünïcödé app") == "n-c-d-app"
    assert len(slug("x" * 100)) == 32
    assert not meetings.is_meeting_id("../etc")


# ─── create / get / update ───────────────────────────────────────────────────────


def test_create_get_update_status(store):
    started = datetime(2026, 9, 12, 9, 30, 0, tzinfo=TZ)
    m = create(store, started_at=started)
    assert isinstance(m, Meeting)
    assert m.status == "recording" and m.schema_version == meetings.SCHEMA_VERSION
    assert m.title == "Zoom — 2026-09-12 09:30"
    assert m.started_at == "2026-09-12T09:30:00+02:00"
    assert m.app_bundle_id == "us.zoom.xos" and m.trigger == "manual"
    assert store.exists(m.id)
    assert (store.path(m.id) / "meeting.json").is_file()
    assert (store.path(m.id) / "audio").is_dir()
    assert not list(store.path(m.id).glob("*.tmp"))

    got = store.get(m.id)
    assert got == m
    assert got is not m  # a copy: mutating it does not poison the cache
    got.title = "changed"
    assert store.get(m.id).title == m.title

    updated = store.update(m.id, title="Standup", ended_at="2026-09-12T10:00:00+02:00")
    assert updated.title == "Standup"
    assert updated.duration_s == 1800.0  # derived from ended_at
    assert store.get(m.id).duration_s == 1800.0

    with pytest.raises(ValueError):
        store.update(m.id, nonsense=1)

    failed = store.set_status(m.id, "failed", error="no audio")
    assert failed.status == "failed" and failed.error == "no audio"
    assert store.get(m.id).error == "no audio"
    with pytest.raises(ValueError):
        store.set_status(m.id, "weird")
    with pytest.raises(KeyError):
        store.update("2026-01-01T00-00-00_nope_000000", title="x")

    assert store.get("missing") is None
    assert store.get("../escape") is None
    with pytest.raises(ValueError):
        store.path("a/b")


def test_explicit_title_and_naive_datetime(store):
    m = create(store, title="Given title", started_at=datetime(2026, 1, 2, 3, 4, 5))
    assert m.title == "Given title"
    assert m.id.startswith("2026-01-02T03-04-05_zoom_")
    assert datetime.fromisoformat(m.started_at).tzinfo is not None


# ─── logs ────────────────────────────────────────────────────────────────────────


def test_segments_and_chunks(store):
    m = create(store)
    store.append_segment(m.id, {"index": 0, "track": "mic", "path": "audio/mic-0.wav", "offset_s": 0})
    store.append_segment(m.id, {"index": 1, "track": "mic", "path": "audio/mic-1.wav", "offset_s": 60})
    assert [s["index"] for s in store.segments_log(m.id)] == [0, 1]

    segs = [Segment(0.5, 1.5, "hello", (Word("hello", 0.5, 1.5),), 0.1)]
    store.append_chunk(m.id, {"index": 0, "track": "mic", "segments": segs})
    store.append_chunk(m.id, {"index": 0, "track": "sys", "segments": []})
    chunks = store.chunks(m.id)
    assert len(chunks) == 2
    assert chunks[0]["track"] == "mic"
    assert chunks[0]["segments"][0] == {
        "start": 0.5,
        "end": 1.5,
        "text": "hello",
        "words": [{"text": "hello", "start": 0.5, "end": 1.5}],
        "no_speech_prob": 0.1,
    }
    assert chunks[1]["segments"] == []

    # A torn line is skipped, not fatal.
    with open(store.path(m.id) / "chunks.jsonl", "a") as fh:
        fh.write('{"index": 2, "tra')
    assert len(store.chunks(m.id)) == 2
    assert store.chunks("2026-01-01T00-00-00_nope_000000") == []


# ─── transcript ──────────────────────────────────────────────────────────────────


def test_write_transcript_files_and_stats(store):
    m = create(store, started_at=datetime(2026, 9, 12, 9, 30, tzinfo=TZ))
    store.update(m.id, duration_s=1500.0)
    done = store.write_transcript(m.id, list(reversed(SEGS)))
    assert done.status == "done"
    assert done.stats["words_total"] == 18
    assert done.stats["words_by_speaker"] == {"You": 11, "Others": 7}
    assert done.stats["talk_time_s_by_speaker"] == {"You": 15.0, "Others": 10.0}
    assert done.stats["segments"] == 3
    assert done.stats["wpm_by_speaker"]["You"] == pytest.approx(44.0)
    assert done.stats["wpm_by_speaker"]["Others"] == pytest.approx(42.0)

    data = json.loads((store.path(m.id) / "transcript.json").read_text())
    assert data["meeting_id"] == m.id
    assert [s["i"] for s in data["segments"]] == [0, 1, 2]
    assert data["segments"][0]["start"] == 0.0 and data["segments"][0]["speaker"] == "You"
    assert set(data["segments"][0]) == {"i", "start", "end", "speaker", "track", "text"}

    md = (store.path(m.id) / "transcript.md").read_text()
    assert md.startswith(f"# {m.title}\n")
    assert "| Duration | 25m 00s |" in md
    assert "| Engine | local / mlx-community/whisper-small-mlx |" in md
    assert "| Words | 18 |" in md
    assert "**[00:00:00] You:** Let us talk about the quarterly roadmap today" in md
    assert "**[00:00:10] Others:** Sure, the budget review comes first though" in md

    assert store.transcript(m.id) == SEGS
    assert store.get(m.id).status == "done"
    assert store.transcript("2026-01-01T00-00-00_nope_000000") == []


# ─── list / search ───────────────────────────────────────────────────────────────


def seed(store):
    now = datetime.now(TZ).replace(microsecond=0)
    old = create(store, app="Zoom", title="Old planning", started_at=now - timedelta(days=30))
    mid = create(store, app="Teams", title="Mid sync", started_at=now - timedelta(days=5))
    new = create(store, app="Zoom", title="New standup", started_at=now - timedelta(hours=1))
    store.write_transcript(old.id, [SpeakerSegment(0, 5, "You", "mic", "we discussed the pineapple pizza question")])
    store.write_transcript(mid.id, SEGS)
    store.update(old.id, duration_s=3600.0)
    store.update(mid.id, duration_s=1800.0)
    store.update(new.id, duration_s=600.0)
    return old, mid, new


def test_list_ordering_paging_days_query(store):
    old, mid, new = seed(store)
    (store.path(new.id) / "audio" / "mic.flac").write_bytes(b"fLaC")

    listing = store.list()
    assert listing["total"] == 3
    assert [i["id"] for i in listing["items"]] == [new.id, mid.id, old.id]
    first = listing["items"][0]
    assert set(first) == {
        "id", "title", "started_at", "ended_at", "duration_s", "app", "backend", "model",
        "status", "words_total", "has_audio",
    }
    assert first["has_audio"] is True and listing["items"][1]["has_audio"] is False
    assert listing["items"][1]["words_total"] == 18
    assert listing["items"][1]["status"] == "done" and first["status"] == "recording"

    page = store.list(limit=1, offset=1)
    assert page["total"] == 3 and [i["id"] for i in page["items"]] == [mid.id]

    recent = store.list(days=7)
    assert [i["id"] for i in recent["items"]] == [new.id, mid.id]

    assert [i["id"] for i in store.list(query="zoom")["items"]] == [new.id, old.id]
    assert [i["id"] for i in store.list(query="standup")["items"]] == [new.id]
    # Transcript hit, not in title or app.
    assert [i["id"] for i in store.list(query="pineapple")["items"]] == [old.id]
    assert [i["id"] for i in store.list(query="budget", days=7)["items"]] == [mid.id]
    assert store.list(query="nothing-matches")["total"] == 0


def test_list_uses_mtime_cache(store, monkeypatch):
    m = create(store)
    store.list()
    calls = []
    real = meetings._read_json

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(meetings, "_read_json", counting)
    store.list()
    assert calls == []  # served from the cache
    store.update(m.id, title="touched")
    store.list()
    assert calls == []  # _save refreshed the cache
    (store.path(m.id) / "meeting.json").write_text(json.dumps({**m.to_dict(), "title": "external"}))
    assert store.list()["items"][0]["title"] == "external"
    assert len(calls) == 1


def test_search_snippets(store):
    old, mid, new = seed(store)
    hits = store.search("budget")
    assert len(hits) == 1
    hit = hits[0]
    assert hit["meeting_id"] == mid.id and hit["title"] == "Mid sync"
    assert hit["start"] == 10.0 and hit["speaker"] == "Others"
    assert hit["text"] == SEGS[1].text
    assert "budget" in hit["snippet"]

    long_text = "x" * 100 + " needle " + "y" * 100
    store.write_transcript(new.id, [SpeakerSegment(0, 1, "You", "mic", long_text)])
    (hit,) = store.search("needle")
    assert hit["snippet"].startswith("…") and hit["snippet"].endswith("…")
    assert len(hit["snippet"]) == 40 + len("needle") + 40 + 2

    # Newest first, limit honoured, regex supported.
    assert [h["meeting_id"] for h in store.search("the")] == [mid.id, mid.id, old.id]
    assert len(store.search("the", limit=2)) == 2
    assert [h["meeting_id"] for h in store.search(r"pine\w+", regex=True)] == [old.id]
    assert store.search("(", regex=True) == []  # bad regex falls back to literal
    assert store.search("   ") == []


# ─── export / delete / audio ─────────────────────────────────────────────────────


def test_export_three_formats(store):
    m = create(store, title="Export me")
    store.write_transcript(m.id, SEGS)

    name, data, mime = store.export(m.id, "md")
    assert name == f"{m.id}.md" and mime.startswith("text/markdown")
    assert data == (store.path(m.id) / "transcript.md").read_bytes()
    assert b"# Export me" in data

    name, data, mime = store.export(m.id, "txt")
    assert name.endswith(".txt") and mime.startswith("text/plain")
    assert data.decode().splitlines()[0] == "[00:00:00] You: Let us talk about the quarterly roadmap today"

    name, data, mime = store.export(m.id, "json")
    assert name.endswith(".json") and mime == "application/json"
    payload = json.loads(data)
    assert payload["id"] == m.id and payload["status"] == "done"
    assert payload["stats"]["words_total"] == 18
    assert len(payload["segments"]) == 3 and payload["segments"][2]["text"] == "Fine by me"

    # md is rendered on the fly when the file is missing.
    (store.path(m.id) / "transcript.md").unlink()
    _, data, _ = store.export(m.id, "md")
    assert b"**[00:00:10] Others:**" in data

    with pytest.raises(ValueError):
        store.export(m.id, "pdf")


def test_delete_and_audio_path(store):
    m = create(store)
    assert store.audio_path(m.id, "mic") is None
    wav = store.path(m.id) / "audio" / "sys.wav"
    wav.write_bytes(b"RIFF")
    assert store.audio_path(m.id, "sys") == wav
    flac = store.path(m.id) / "audio" / "sys.flac"
    flac.write_bytes(b"fLaC")
    assert store.audio_path(m.id, "sys") == flac  # flac preferred

    assert store.delete(m.id) is True
    assert not store.path(m.id).exists()
    assert store.get(m.id) is None
    assert store.delete(m.id) is False
    assert store.list()["total"] == 0


# ─── stats / robustness ──────────────────────────────────────────────────────────


def test_stats_aggregation(store):
    old, mid, new = seed(store)
    s = store.stats()
    assert s["meetings"] == 3
    assert s["hours_total"] == pytest.approx(1.67, abs=0.01)
    assert s["avg_minutes"] == pytest.approx(33.3, abs=0.1)
    assert s["words_total"] == 18 + 6
    assert s["by_app"] == [
        {"app": "Zoom", "meetings": 2, "minutes": 70.0},
        {"app": "Teams", "meetings": 1, "minutes": 30.0},
    ]
    assert sum(w["meetings"] for w in s["by_week"]) == 3
    for w in s["by_week"]:
        assert datetime.fromisoformat(w["week_start"]).weekday() == 0
    assert s["by_week"] == sorted(s["by_week"], key=lambda w: w["week_start"])
    # talk time: old = You 5s; mid = You 15s + Others 10s -> You 20 / Others 10
    assert s["talk_time"] == {"You": 66.7, "Others": 33.3}
    assert [l["id"] for l in s["longest"]] == [old.id, mid.id, new.id]
    assert s["longest"][0] == {"id": old.id, "title": "Old planning", "minutes": 60.0}

    week = store.stats(days=7)
    assert week["meetings"] == 2 and week["words_total"] == 18
    assert week["talk_time"] == {"You": 60.0, "Others": 40.0}

    empty = MeetingStore(store.root / "none").stats()
    assert empty["meetings"] == 0 and empty["avg_minutes"] == 0.0
    assert empty["talk_time"] == {"You": 0.0, "Others": 0.0}
    assert empty["longest"] == [] and empty["by_app"] == []


def test_corrupt_meeting_skipped_once(store, capsys):
    good = create(store)
    bad_dir = store.root / "2026-01-01T00-00-00_broken_abcdef"
    bad_dir.mkdir()
    (bad_dir / "meeting.json").write_text("{not json")
    (store.root / "2026-01-02T00-00-00_nofile_abcdef").mkdir()
    (store.root / "stray.txt").write_text("ignore me")

    assert [i["id"] for i in store.list()["items"]] == [good.id]
    assert store.get("2026-01-01T00-00-00_broken_abcdef") is None
    store.list()
    out = capsys.readouterr().out
    assert out.count("Warning: skipping unreadable meeting record") == 1
    assert store.stats()["meetings"] == 1
    assert store.search("anything") == []

    # Missing required fields is also just skipped; extra keys are tolerated.
    (bad_dir / "meeting.json").write_text(json.dumps({"title": "no id"}))
    assert store.get("2026-01-01T00-00-00_broken_abcdef") is None
    (bad_dir / "meeting.json").write_text(
        json.dumps({"id": "wrong", "started_at": "2026-01-01T00:00:00", "future_field": 1, "tracks": "junk"})
    )
    m = store.get("2026-01-01T00-00-00_broken_abcdef")
    assert m is not None and m.id == "2026-01-01T00-00-00_broken_abcdef"
    assert m.tracks == [] and m.title == "wrong"


def test_meeting_round_trip():
    m = Meeting(id="x", title="t", started_at="2026-01-01T00:00:00+00:00", tracks=[{"track": "mic"}], stats={"words_total": 3})
    assert Meeting.from_dict(json.loads(json.dumps(m.to_dict()))) == m
    assert m.words_total == 3
    assert m.started == datetime(2026, 1, 1, tzinfo=timezone.utc)
