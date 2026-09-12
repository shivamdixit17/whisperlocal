"""
Meeting storage: one directory per meeting under settings.meeting_root.

    <YYYY-MM-DDTHH-MM-SS>_<appslug>_<6hex>/
        meeting.json       the Meeting record (status, stats, provenance)
        segments.jsonl     the recorder's log of audio chunks written
        chunks.jsonl       transcribed chunks, appended as the worker finishes them
        transcript.json    the merged, speaker-labelled transcript
        transcript.md      the same, rendered for humans
        audio/             mic.flac / system.flac (or .wav) if audio is kept

Plain files rather than a database on purpose: a user can open a folder,
read the Markdown, and delete a meeting with the Finder. Writes go through
a temp file and os.replace so a crash never leaves half a meeting.json
behind, and every reader tolerates a broken or missing file.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import tempfile
from collections import defaultdict
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, Literal

from whisperlocal.transcription import longform
from whisperlocal.transcription.longform import SpeakerSegment

SCHEMA_VERSION = 1
STATUSES = ("recording", "transcribing", "done", "failed")
TRIGGERS = ("manual", "prompt", "auto")

MEETING_FILE = "meeting.json"
SEGMENTS_FILE = "segments.jsonl"
CHUNKS_FILE = "chunks.jsonl"
TRANSCRIPT_JSON = "transcript.json"
TRANSCRIPT_MD = "transcript.md"
AUDIO_DIR = "audio"
AUDIO_EXTENSIONS = (".flac", ".wav")

_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}_[a-z0-9-]+_[0-9a-f]{6}$")


# ─── Ids ─────────────────────────────────────────────────────────────────────────


def slug(text: str | None) -> str:
    """Lowercase, ASCII, dashes; never empty."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:32].strip("-") or "meeting"


def new_meeting_id(started_at: datetime, app: str | None) -> str:
    stamp = started_at.strftime("%Y-%m-%dT%H-%M-%S")
    return f"{stamp}_{slug(app)}_{secrets.token_hex(3)}"


def is_meeting_id(value: str) -> bool:
    return bool(_ID_RE.match(value or ""))


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ─── Record ──────────────────────────────────────────────────────────────────────


@dataclass
class Meeting:
    id: str
    title: str
    started_at: str
    ended_at: str | None = None
    duration_s: float | None = None
    app: str | None = None
    app_bundle_id: str | None = None
    trigger: str = "manual"
    backend: str = "local"
    model: str = ""
    language: str | None = None
    status: str = "recording"
    error: str | None = None
    tracks: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Meeting":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in d.items() if k in known}
        if "id" not in data or "started_at" not in data:
            raise ValueError("meeting record is missing id or started_at")
        data.setdefault("title", data["id"])
        if not isinstance(data.get("tracks"), list):
            data["tracks"] = []
        if not isinstance(data.get("stats"), dict):
            data["stats"] = {}
        return cls(**data)

    @property
    def started(self) -> datetime | None:
        return _parse_iso(self.started_at)

    @property
    def words_total(self) -> int:
        return int(self.stats.get("words_total", 0) or 0)


# ─── Atomic file helpers ─────────────────────────────────────────────────────────


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_json(path: Path, obj: object) -> None:
    _write_atomic(path, json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"))


def _read_json(path: Path) -> object | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()


def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        pass
    return out


# ─── Store ───────────────────────────────────────────────────────────────────────


class MeetingStore:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser()
        # id -> (mtime_ns, Meeting) so list() rereads only what changed.
        self._cache: dict[str, tuple[int, Meeting]] = {}
        self._complained: set[str] = set()

    # ── paths ────────────────────────────────────────────────────────────────

    def path(self, id: str) -> Path:
        if not id or "/" in id or id in (".", "..") or "\\" in id:
            raise ValueError(f"bad meeting id {id!r}")
        return self.root / id

    def exists(self, id: str) -> bool:
        try:
            return (self.path(id) / MEETING_FILE).is_file()
        except ValueError:
            return False

    def audio_path(self, id: str, track: str) -> Path | None:
        for ext in AUDIO_EXTENSIONS:
            candidate = self.path(id) / AUDIO_DIR / f"{track}{ext}"
            if candidate.is_file():
                return candidate
        return None

    def audio_dir(self, id: str) -> Path:
        return self.path(id) / AUDIO_DIR

    # ── create / read / update ───────────────────────────────────────────────

    def create(
        self,
        *,
        app: str | None,
        app_bundle_id: str | None,
        trigger: str,
        backend: str,
        model: str,
        language: str | None,
        title: str | None = None,
        started_at: datetime | None = None,
    ) -> Meeting:
        started = started_at or _now()
        if started.tzinfo is None:
            started = started.astimezone()
        self.root.mkdir(parents=True, exist_ok=True)
        for _ in range(5):
            id = new_meeting_id(started, app)
            if not (self.root / id).exists():
                break
        meeting = Meeting(
            id=id,
            title=title or f"{app or 'Meeting'} — {started.strftime('%Y-%m-%d %H:%M')}",
            started_at=_iso(started),
            app=app,
            app_bundle_id=app_bundle_id,
            trigger=trigger if trigger in TRIGGERS else "manual",
            backend=backend,
            model=model,
            language=language,
            status="recording",
        )
        (self.root / id / AUDIO_DIR).mkdir(parents=True, exist_ok=True)
        self._save(meeting)
        return meeting

    def get(self, id: str) -> Meeting | None:
        try:
            path = self.path(id) / MEETING_FILE
        except ValueError:
            return None
        return self._load(id, path)

    def update(self, id: str, **fields_) -> Meeting:
        meeting = self._require(id)
        known = {f.name for f in fields(Meeting)}
        for key, value in fields_.items():
            if key not in known or key == "id":
                raise ValueError(f"cannot set {key!r} on a meeting")
            setattr(meeting, key, value)
        if meeting.ended_at and meeting.duration_s is None:
            a, b = _parse_iso(meeting.started_at), _parse_iso(meeting.ended_at)
            if a and b:
                meeting.duration_s = max(0.0, (b - a).total_seconds())
        self._save(meeting)
        return meeting

    def set_status(self, id: str, status: str, error: str | None = None) -> Meeting:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}, not {status!r}")
        return self.update(id, status=status, error=error)

    def delete(self, id: str) -> bool:
        path = self.path(id)
        self._cache.pop(id, None)
        if not path.is_dir():
            return False
        shutil.rmtree(path, ignore_errors=True)
        return not path.exists()

    # ── logs ─────────────────────────────────────────────────────────────────

    def append_segment(self, id: str, info: dict) -> None:
        _append_jsonl(self.path(id) / SEGMENTS_FILE, dict(info))

    def segments_log(self, id: str) -> list[dict]:
        return _read_jsonl(self.path(id) / SEGMENTS_FILE)

    def append_chunk(self, id: str, chunk: dict) -> None:
        """chunk: {"index", "track", "segments": [Segment or dict, ...]}."""
        record = dict(chunk)
        record["segments"] = [
            asdict(s) if not isinstance(s, dict) else s for s in chunk.get("segments", [])
        ]
        _append_jsonl(self.path(id) / CHUNKS_FILE, record)

    def chunks(self, id: str) -> list[dict]:
        return _read_jsonl(self.path(id) / CHUNKS_FILE)

    # ── transcript ───────────────────────────────────────────────────────────

    def write_transcript(self, id: str, segments: list[SpeakerSegment]) -> Meeting:
        meeting = self._require(id)
        segments = sorted(segments, key=lambda s: (s.start, s.end))
        payload = {
            "meeting_id": id,
            "segments": [
                {"i": i, **s.to_dict()} for i, s in enumerate(segments)
            ],
        }
        _write_json(self.path(id) / TRANSCRIPT_JSON, payload)

        stats = compute_stats(segments)
        meeting.stats = {**meeting.stats, **stats}
        meeting.status = "done"
        meeting.error = None
        _write_atomic(
            self.path(id) / TRANSCRIPT_MD,
            self._render_markdown(meeting, segments).encode("utf-8"),
        )
        self._save(meeting)
        return meeting

    def transcript(self, id: str) -> list[SpeakerSegment]:
        data = _read_json(self.path(id) / TRANSCRIPT_JSON)
        if not isinstance(data, dict):
            return []
        out: list[SpeakerSegment] = []
        for item in data.get("segments", []):
            if isinstance(item, dict):
                try:
                    out.append(SpeakerSegment.from_dict(item))
                except (TypeError, ValueError):
                    continue
        return out

    def has_transcript(self, id: str) -> bool:
        return (self.path(id) / TRANSCRIPT_JSON).is_file()

    def _render_markdown(self, meeting: Meeting, segments: list[SpeakerSegment]) -> str:
        meta = {
            "started_at": _human_time(meeting.started_at),
            "duration_s": meeting.duration_s,
            "app": meeting.app,
            "backend": meeting.backend,
            "model": meeting.model,
            "words": meeting.stats.get("words_total"),
        }
        return longform.render_markdown(meeting.title, meta, segments)

    # ── listing and search ───────────────────────────────────────────────────

    def _iter_meetings(self) -> Iterator[Meeting]:
        """Every readable meeting, newest first."""
        if not self.root.is_dir():
            return
        try:
            names = sorted((p.name for p in self.root.iterdir() if p.is_dir()), reverse=True)
        except OSError:
            return
        for name in names:
            meeting = self._load(name, self.root / name / MEETING_FILE)
            if meeting is not None:
                yield meeting

    def all(self) -> list[Meeting]:
        return list(self._iter_meetings())

    def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        days: int | None = None,
        query: str | None = None,
    ) -> dict:
        cutoff = _cutoff(days)
        q = (query or "").strip().lower()
        hits: set[str] = set()
        if q:
            hits = {h["meeting_id"] for h in self.search(q, limit=10_000)}

        matched: list[Meeting] = []
        for meeting in self._iter_meetings():
            if cutoff and not _after(meeting.started_at, cutoff):
                continue
            if q:
                haystack = f"{meeting.title} {meeting.app or ''}".lower()
                if q not in haystack and meeting.id not in hits:
                    continue
            matched.append(meeting)

        page = matched[offset : offset + limit] if limit > 0 else matched[offset:]
        return {"total": len(matched), "items": [self.summary(m) for m in page]}

    def summary(self, meeting: Meeting) -> dict:
        return {
            "id": meeting.id,
            "title": meeting.title,
            "started_at": meeting.started_at,
            "ended_at": meeting.ended_at,
            "duration_s": meeting.duration_s,
            "app": meeting.app,
            "backend": meeting.backend,
            "model": meeting.model,
            "status": meeting.status,
            "words_total": meeting.words_total,
            "has_audio": self._has_audio(meeting.id),
        }

    def _has_audio(self, id: str) -> bool:
        audio = self.root / id / AUDIO_DIR
        try:
            return any(p.suffix in AUDIO_EXTENSIONS for p in audio.iterdir())
        except OSError:
            return False

    def search(self, query: str, *, limit: int = 50, regex: bool = False) -> list[dict]:
        """Hits across transcript.json files, newest meeting first."""
        query = (query or "").strip()
        if not query:
            return []
        if regex:
            try:
                pattern = re.compile(query, re.IGNORECASE)
            except re.error:
                pattern = re.compile(re.escape(query), re.IGNORECASE)
        else:
            pattern = re.compile(re.escape(query), re.IGNORECASE)

        out: list[dict] = []
        for meeting in self._iter_meetings():
            if len(out) >= limit:
                break
            for seg in self.transcript(meeting.id):
                m = pattern.search(seg.text)
                if not m:
                    continue
                out.append(
                    {
                        "meeting_id": meeting.id,
                        "title": meeting.title,
                        "started_at": meeting.started_at,
                        "start": seg.start,
                        "speaker": seg.speaker,
                        "text": seg.text,
                        "snippet": _snippet(seg.text, m.start(), m.end()),
                    }
                )
                if len(out) >= limit:
                    break
        return out

    # ── export ───────────────────────────────────────────────────────────────

    def export(self, id: str, fmt: Literal["md", "txt", "json"]) -> tuple[str, bytes, str]:
        meeting = self._require(id)
        base = f"{meeting.id}"
        if fmt == "md":
            path = self.path(id) / TRANSCRIPT_MD
            if path.is_file():
                data = path.read_bytes()
            else:
                data = self._render_markdown(meeting, self.transcript(id)).encode("utf-8")
            return f"{base}.md", data, "text/markdown; charset=utf-8"
        if fmt == "txt":
            data = longform.render_text(self.transcript(id)).encode("utf-8")
            return f"{base}.txt", data, "text/plain; charset=utf-8"
        if fmt == "json":
            payload = meeting.to_dict()
            payload["segments"] = [
                {"i": i, **s.to_dict()} for i, s in enumerate(self.transcript(id))
            ]
            data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            return f"{base}.json", data, "application/json"
        raise ValueError(f"unknown export format {fmt!r}")

    # ── stats ────────────────────────────────────────────────────────────────

    def stats(self, *, days: int | None = None) -> dict:
        cutoff = _cutoff(days)
        meetings = [
            m
            for m in self._iter_meetings()
            if not cutoff or _after(m.started_at, cutoff)
        ]
        total_s = sum(float(m.duration_s or 0.0) for m in meetings)
        words_total = sum(m.words_total for m in meetings)

        by_app: dict[str, dict] = defaultdict(lambda: {"meetings": 0, "minutes": 0.0})
        by_week: dict[str, dict] = defaultdict(lambda: {"meetings": 0, "minutes": 0.0})
        talk: dict[str, float] = defaultdict(float)
        for m in meetings:
            minutes = float(m.duration_s or 0.0) / 60.0
            app = m.app or "Unknown"
            by_app[app]["meetings"] += 1
            by_app[app]["minutes"] += minutes
            started = m.started
            if started is not None:
                week = (started.date() - timedelta(days=started.weekday())).isoformat()
                by_week[week]["meetings"] += 1
                by_week[week]["minutes"] += minutes
            for speaker, seconds in (m.stats.get("talk_time_s_by_speaker") or {}).items():
                try:
                    talk[str(speaker)] += float(seconds)
                except (TypeError, ValueError):
                    continue

        talk_total = sum(talk.values())
        talk_pct = {
            speaker: (round(100.0 * s / talk_total, 1) if talk_total else 0.0)
            for speaker, s in talk.items()
        }
        for name in (longform.SPEAKER_MIC, longform.SPEAKER_SYS):
            talk_pct.setdefault(name, 0.0)

        longest = sorted(meetings, key=lambda m: float(m.duration_s or 0.0), reverse=True)[:5]
        return {
            "meetings": len(meetings),
            "hours_total": round(total_s / 3600.0, 2),
            "avg_minutes": round(total_s / 60.0 / len(meetings), 1) if meetings else 0.0,
            "words_total": words_total,
            "by_app": sorted(
                (
                    {"app": app, "meetings": v["meetings"], "minutes": round(v["minutes"], 1)}
                    for app, v in by_app.items()
                ),
                key=lambda r: (-r["minutes"], r["app"]),
            ),
            "by_week": sorted(
                (
                    {"week_start": week, "meetings": v["meetings"], "minutes": round(v["minutes"], 1)}
                    for week, v in by_week.items()
                ),
                key=lambda r: r["week_start"],
            ),
            "talk_time": talk_pct,
            "longest": [
                {
                    "id": m.id,
                    "title": m.title,
                    "minutes": round(float(m.duration_s or 0.0) / 60.0, 1),
                }
                for m in longest
            ],
        }

    # ── internals ────────────────────────────────────────────────────────────

    def _require(self, id: str) -> Meeting:
        meeting = self.get(id)
        if meeting is None:
            raise KeyError(f"no meeting {id!r}")
        return meeting

    def _save(self, meeting: Meeting) -> None:
        path = self.path(meeting.id) / MEETING_FILE
        _write_json(path, meeting.to_dict())
        try:
            self._cache[meeting.id] = (path.stat().st_mtime_ns, meeting)
        except OSError:
            self._cache.pop(meeting.id, None)

    def _load(self, id: str, path: Path) -> Meeting | None:
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            self._cache.pop(id, None)
            return None
        cached = self._cache.get(id)
        if cached and cached[0] == mtime:
            return _copy(cached[1])

        data = _read_json(path)
        meeting: Meeting | None = None
        if isinstance(data, dict):
            try:
                meeting = Meeting.from_dict(data)
            except (TypeError, ValueError):
                meeting = None
        if meeting is None:
            if id not in self._complained:
                self._complained.add(id)
                print(f"Warning: skipping unreadable meeting record {path}")
            self._cache.pop(id, None)
            return None
        if meeting.id != id:
            meeting.id = id  # the directory name is the source of truth
        self._cache[id] = (mtime, meeting)
        return _copy(meeting)


# ─── Helpers ─────────────────────────────────────────────────────────────────────


def _copy(meeting: Meeting) -> Meeting:
    return Meeting.from_dict(json.loads(json.dumps(meeting.to_dict())))


def compute_stats(segments: list[SpeakerSegment]) -> dict:
    words_by: dict[str, int] = defaultdict(int)
    time_by: dict[str, float] = defaultdict(float)
    for s in segments:
        words_by[s.speaker] += longform.word_count(s.text)
        time_by[s.speaker] += s.duration
    wpm = {
        spk: (round(words_by[spk] / (time_by[spk] / 60.0), 1) if time_by[spk] > 0 else 0.0)
        for spk in words_by
    }
    return {
        "words_total": sum(words_by.values()),
        "words_by_speaker": dict(words_by),
        "talk_time_s_by_speaker": {k: round(v, 2) for k, v in time_by.items()},
        "segments": len(segments),
        "wpm_by_speaker": wpm,
    }


def _cutoff(days: int | None) -> datetime | None:
    if days is None or days <= 0:
        return None
    return _now() - timedelta(days=days)


def _after(started_at: str, cutoff: datetime) -> bool:
    started = _parse_iso(started_at)
    if started is None:
        return False
    if started.tzinfo is None:
        started = started.astimezone()
    return started >= cutoff


def _human_time(iso: str) -> str:
    dt = _parse_iso(iso)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else iso


def _snippet(text: str, start: int, end: int, radius: int = 40) -> str:
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    prefix = "…" if lo > 0 else ""
    suffix = "…" if hi < len(text) else ""
    return f"{prefix}{text[lo:hi]}{suffix}"


__all__ = [
    "AUDIO_DIR",
    "Meeting",
    "MeetingStore",
    "SCHEMA_VERSION",
    "STATUSES",
    "TRIGGERS",
    "compute_stats",
    "is_meeting_id",
    "new_meeting_id",
    "slug",
]
