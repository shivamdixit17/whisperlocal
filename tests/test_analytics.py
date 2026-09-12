"""whisperlocal.analytics — pure functions over synthetic history entries.

Fixed clock: NOW is Saturday 2026-09-12 12:00 at UTC+2. Monday of that ISO
week is 2026-09-07.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from whisperlocal import analytics as A
from whisperlocal import stats

TZ = timezone(timedelta(hours=2))
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=TZ)
MODEL = "mlx-community/whisper-base-mlx"


def mk(ts, status="ok", words=10, secs=5.0, ms=200, app="Code", text="hello there world",
       model=MODEL, backend=None, wpm=None, bundle="com.microsoft.VSCode"):
    if isinstance(ts, datetime):
        ts = ts.isoformat()
    e = {
        "timestamp": ts,
        "status": status,
        "text": text if status == "ok" else None,
        "words": words if status == "ok" else 0,
        "chars": len(text) if (status == "ok" and text) else 0,
        "audio_seconds": secs,
        "transcribe_ms": ms,
        "wpm": (wpm if wpm is not None else (round(words / secs * 60, 1) if secs else None))
        if status == "ok" else None,
        "app": app,
        "app_bundle_id": bundle if app else None,
        "model": model,
    }
    if backend is not None:
        e["backend"] = backend
    return e


def at(day_offset, hour=10, minute=0, tz=TZ):
    """A datetime `day_offset` days before NOW's date, at the given local time."""
    d = NOW.date() - timedelta(days=day_offset)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz)


# ---------------------------------------------------------------- helpers ---


def test_percentile_empty_single_interpolation():
    assert A.percentile([], 50) is None
    assert A.percentile([7], 90) == 7
    assert A.percentile([1, 2, 3, 4], 50) == 2.5
    assert A.percentile([10, 20, 30, 40, 50], 25) == 20
    assert A.percentile([10, 20, 30, 40, 50], 90) == pytest.approx(46)
    assert A.percentile([0, 100], 100) == 100
    assert A.percentile([0, 100], 0) == 0
    # junk is ignored, not fatal
    assert A.percentile([3, None, "x", 1], 50) == 2


def test_entry_time_bad_input():
    assert A.entry_time({"timestamp": "2026-09-12T10:00:30+02:00"}) == datetime(
        2026, 9, 12, 10, 0, 30, tzinfo=TZ)
    assert A.entry_time({"timestamp": "not a date"}) is None
    assert A.entry_time({"timestamp": None}) is None
    assert A.entry_time({}) is None
    assert A.entry_time("2026-09-12T10:00:30+02:00") is None
    assert A.entry_time(None) is None


def test_short_model():
    assert A.short_model("mlx-community/whisper-base-mlx") == "whisper-base"
    assert A.short_model("mlx-community/whisper-large-v3-turbo") == "whisper-large-v3-turbo"
    assert A.short_model("whisper-1") == "whisper-1"
    assert A.short_model(None) == "unknown"


# ----------------------------------------------------------------- window ---


def test_filter_window_calendar_days_and_bad_entries():
    entries = [
        mk(at(0, 9)),            # today
        mk(at(6, 23, 59)),       # 6 days ago, late -> inside days=7
        mk(at(7, 0, 1)),         # 7 days ago -> outside days=7
        {"timestamp": "garbage"},
        "not even a dict",
    ]
    seven = A.filter_window(entries, 7, NOW)
    assert [e["timestamp"] for e in seven] == [entries[0]["timestamp"], entries[1]["timestamp"]]
    assert len(A.filter_window(entries, 1, NOW)) == 1
    # None keeps everything with a parseable timestamp
    assert len(A.filter_window(entries, None, NOW)) == 3


# ----------------------------------------------------------------- totals ---


def test_totals_including_minutes_saved():
    entries = [
        mk(at(0), words=100, secs=60.0, ms=1000),
        mk(at(0), words=100, secs=60.0, ms=1000),
        mk(at(0), status="empty", secs=1.0, ms=50),
        mk(at(0), status="too_short", secs=0.3, ms=None),
    ]
    t = A.totals(entries, typing_wpm=40.0)
    assert t["dictations"] == 4
    assert t["ok"] == 2
    assert t["words"] == 200
    assert t["minutes_speaking"] == 2.0
    assert t["avg_words"] == 100.0
    assert t["avg_seconds"] == 60.0
    assert t["avg_wpm"] == 100.0
    # 200 words / 40 wpm = 5 min typing; spent 120 s audio + 2 s transcribe = 2.033 min
    assert t["minutes_saved"] == round(5.0 - 122.0 / 60.0, 1)


def test_totals_can_be_negative_and_empty():
    slow = [mk(at(0), words=1, secs=120.0, ms=0)]
    assert A.totals(slow, 40.0)["minutes_saved"] < 0
    t = A.totals([], 40.0)
    assert t["dictations"] == 0 and t["avg_words"] is None and t["minutes_saved"] == 0.0


# ------------------------------------------------------------------ daily ---


def test_daily_series_zero_fill_and_length():
    entries = [mk(at(6), words=5), mk(at(6), words=7), mk(at(0), words=1),
               mk(at(0), status="empty")]
    rows = A.daily_series(entries, 7, NOW)
    assert len(rows) == 7
    assert rows[0]["date"] == (NOW.date() - timedelta(days=6)).isoformat()
    assert rows[-1]["date"] == NOW.date().isoformat()
    assert rows[0] == {"date": rows[0]["date"], "dictations": 2, "ok": 2, "words": 12,
                       "minutes": round(10 / 60, 1)}
    assert all(r["dictations"] == 0 for r in rows[1:-1])       # the gap is zero-filled
    assert rows[-1]["dictations"] == 2 and rows[-1]["ok"] == 1 and rows[-1]["words"] == 1


def test_daily_series_all_time_starts_at_first_entry():
    entries = [mk(at(10)), mk(at(2))]
    rows = A.daily_series(entries, None, NOW)
    assert len(rows) == 11
    assert rows[0]["date"] == at(10).date().isoformat()
    assert A.daily_series([], None, NOW) == []


# ----------------------------------------------------------------- weekly ---


def test_weekly_series_delta():
    # weeks: w-3 (empty), w-2 = 100 words, w-1 = 150 words, w0 = 0 words
    entries = [
        mk(at(14), words=100),                  # Sat two weeks ago
        mk(at(7), words=150),                   # Sat last week
        mk(at(0), status="empty"),              # this week, no words
    ]
    rows = A.weekly_series(entries, weeks=4, now=NOW)
    assert len(rows) == 4
    assert [r["week_start"] for r in rows] == [
        "2026-08-17", "2026-08-24", "2026-08-31", "2026-09-07"]
    assert rows[0]["delta_pct"] is None                     # first
    assert rows[1]["delta_pct"] is None                     # previous was 0
    assert rows[2]["delta_pct"] == 50.0
    assert rows[3]["delta_pct"] == -100.0
    assert rows[3]["dictations"] == 1 and rows[3]["words"] == 0


# ---------------------------------------------------------------- heatmap ---


def test_heatmap_indices_in_own_offset():
    monday = datetime(2026, 9, 7, 10, 0, tzinfo=TZ)
    assert monday.weekday() == 0
    monday_pacific = datetime(2026, 9, 7, 10, 0, tzinfo=timezone(timedelta(hours=-7)))
    sunday_night = datetime(2026, 9, 13, 23, 30, tzinfo=TZ)
    h = A.hour_weekday_heatmap([mk(monday), mk(monday_pacific), mk(sunday_night),
                                {"timestamp": "bad"}])
    assert h["rows"][0] == "Mon" and h["rows"][6] == "Sun"
    assert h["cols"] == list(range(24))
    assert h["counts"][0][10] == 2
    assert h["counts"][6][23] == 1
    assert h["max"] == 2
    assert sum(map(sum, h["counts"])) == 3


# -------------------------------------------------------------------- wpm ---


def test_wpm_distribution_bins_and_p50():
    entries = [mk(at(0), wpm=w) for w in (10, 30, 50, 70, 90)]
    entries.append(mk(at(0), status="empty"))              # ignored
    entries.append(mk(at(0), wpm=0))                       # ignored (no rate)
    d = A.wpm_distribution(entries, bin_size=20)
    assert d["p50"] == 50.0
    assert d["mean"] == 50.0
    assert d["n"] == 5
    assert [b["count"] for b in d["bins"]] == [1, 1, 1, 1, 1]
    assert d["bins"][0] == {"from": 0, "to": 20, "count": 1}
    assert d["bins"][-1]["to"] == 100
    assert d["trend"] == [{"week_start": "2026-09-07", "median_wpm": 50.0, "n": 5}]
    assert A.wpm_distribution([]) == {"bins": [], "p50": None, "p90": None, "mean": None,
                                      "trend": []}


# ---------------------------------------------------------------- latency ---


def test_latency_by_model_shortening_and_order():
    entries = [mk(at(0), ms=100), mk(at(0), ms=200), mk(at(0), ms=900),
               mk(at(0), ms=50, model="whisper-1", backend="api"),
               mk(at(0), status="too_short", ms=None)]
    rows = A.latency_by_model(entries)
    assert [r["short"] for r in rows] == ["whisper-base", "whisper-1"]
    base = rows[0]
    assert base["model"] == MODEL
    assert base["n"] == 3 and base["p50_ms"] == 200 and base["max_ms"] == 900
    assert base["mean_ms"] == 400
    assert isinstance(base["p95_ms"], int)


# --------------------------------------------------------------- outcomes ---


def test_outcomes_rates_and_trend():
    entries = [mk(at(7)), mk(at(7), status="hallucination"),
               mk(at(0)), mk(at(0)), mk(at(0), status="too_short"), mk(at(0), status="empty")]
    o = A.outcomes(entries)
    assert o["total"] == 6
    assert o["counts"] == {"ok": 3, "empty": 1, "hallucination": 1, "too_short": 1}
    assert o["rates"]["ok"] == 50.0 and o["rates"]["hallucination"] == round(100 / 6, 1)
    assert [t["week_start"] for t in o["trend"]] == ["2026-08-31", "2026-09-07"]
    assert o["trend"][0]["hallucination_pct"] == 50.0
    assert o["trend"][1] == {"week_start": "2026-09-07", "total": 4, "ok_pct": 50.0,
                             "hallucination_pct": 0.0, "too_short_pct": 25.0,
                             "empty_pct": 25.0}


# ----------------------------------------------------------------- by_app ---


def test_by_app_ordering_and_none_app():
    entries = [mk(at(0), app="Code"), mk(at(0), app="Code"),
               mk(at(0), app="Code", status="hallucination"),
               mk(at(0), app=None), mk(at(0), app="Slack", bundle="com.tinyspeck.slackmacgap")]
    rows = A.by_app(entries)
    assert [r["app"] for r in rows] == ["Code", "Slack", "unknown"]
    code = rows[0]
    assert code["dictations"] == 3 and code["ok"] == 2
    assert code["bundle_id"] == "com.microsoft.VSCode"
    assert code["hallucination_pct"] == round(100 / 3, 1) and code["too_short_pct"] == 0.0
    assert rows[2]["bundle_id"] is None
    assert len(A.by_app(entries, limit=2)) == 2


def test_app_name_strips_invisible_marks_consistently():
    entries = [mk(at(0), app="\u200eWhatsApp"), mk(at(0), app=" WhatsApp ")]
    rows = A.by_app(entries)
    assert [r["app"] for r in rows] == ["WhatsApp"] and rows[0]["dictations"] == 2
    assert A.recent(entries, app="WhatsApp")["total"] == 2


# ---------------------------------------------------------------- lengths ---


def test_length_histogram_edges():
    secs = [0.0, 1.99, 2.0, 4.99, 5.0, 10.0, 19.9, 20.0, 40.0, 59.99, 60.0, 300.0]
    entries = [mk(at(0), secs=s) for s in secs] + [mk(at(0), secs=None), mk(at(0), secs=-1)]
    h = A.length_histogram(entries)
    labels = [b["label"] for b in h["buckets"]]
    assert labels == ["0-2s", "2-5s", "5-10s", "10-20s", "20-40s", "40-60s", "60s+"]
    assert [b["count"] for b in h["buckets"]] == [2, 2, 1, 2, 1, 2, 2]
    assert h["buckets"][-1]["to"] is None


# ---------------------------------------------------------------- streaks ---


def test_streaks_yesterday_anchor_broken_and_longest():
    # active: yesterday, day-2, day-3 (current = 3, anchored on yesterday)
    # earlier run: day-10..day-6 (5 days) -> longest = 5
    entries = [mk(at(d)) for d in (1, 2, 3, 6, 7, 8, 9, 10)]
    entries.append(mk(at(0), status="empty"))               # today has no ok -> not active
    s = A.streaks(entries, NOW)
    assert s == {"current_days": 3, "longest_days": 5, "active_days": 8,
                 "last_active": at(1).date().isoformat()}


def test_streaks_broken_and_empty():
    s = A.streaks([mk(at(2)), mk(at(3))], NOW)                # nothing today or yesterday
    assert s["current_days"] == 0 and s["longest_days"] == 2
    assert A.streaks([], NOW) == {"current_days": 0, "longest_days": 0, "active_days": 0,
                                  "last_active": None}
    today_only = A.streaks([mk(at(0))], NOW)
    assert today_only["current_days"] == 1


# -------------------------------------------------------------- top_terms ---


def test_top_terms_stopwords_min_len_and_has_text():
    entries = [
        mk(at(0), text="Please fix the menu bar icon, the menu bar icon is wrong."),
        mk(at(0), text="Menu bar icon again at 10 o'clock; it's 42."),
        mk(at(0), status="empty"),
    ]
    t = A.top_terms(entries, n=5, min_len=3)
    uni = {u["term"]: u["count"] for u in t["unigrams"]}
    assert uni["menu"] == 3 and uni["bar"] == 3 and uni["icon"] == 3
    assert "the" not in uni and "is" not in uni and "it's" not in uni
    assert "42" not in uni and "10" not in uni
    assert "at" not in uni                                   # shorter than min_len
    assert t["bigrams"][0] == {"term": "bar icon", "count": 3} or \
        t["bigrams"][0] == {"term": "menu bar", "count": 3}
    assert all(" the " not in b["term"] for b in t["bigrams"])
    assert len(t["unigrams"]) <= 5
    assert t["has_text"] is True

    none = A.top_terms([mk(at(0), text=None), mk(at(0), text="")])
    assert none == {"unigrams": [], "bigrams": [], "has_text": False}


# ---------------------------------------------------------- model/backend ---


def test_model_usage():
    entries = [mk(at(7)), mk(at(0)), mk(at(0)),
               mk(at(0), model="whisper-1", backend="api")]
    rows = A.model_usage(entries)
    assert rows == [
        {"week_start": "2026-08-31", "model_short": "whisper-base", "dictations": 1},
        {"week_start": "2026-09-07", "model_short": "whisper-base", "dictations": 2},
        {"week_start": "2026-09-07", "model_short": "whisper-1", "dictations": 1},
    ]


def test_backend_usage_defaults_to_local():
    entries = [mk(at(0), secs=10.0), mk(at(0), secs=5.0, backend="local"),
               mk(at(0), secs=2.5, backend="api"), mk(at(0), secs=None, backend="api")]
    b = A.backend_usage(entries)
    assert b["local"] == {"dictations": 2, "audio_seconds": 15.0}
    assert b["api"] == {"dictations": 2, "audio_seconds": 2.5}
    assert A.backend_usage([]) == {"local": {"dictations": 0, "audio_seconds": 0.0},
                                   "api": {"dictations": 0, "audio_seconds": 0.0}}


# ----------------------------------------------------------------- recent ---


def test_recent_filtering_and_paging():
    entries = [
        mk(at(3), text="alpha one", app="Code"),
        mk(at(2), text="Alpha two", app="Slack"),
        mk(at(1), status="empty", app="Code"),
        mk(at(0), text="beta", app="Code"),
    ]
    r = A.recent(entries)
    assert r["total"] == 4 and r["has_text"] is True
    assert [i["timestamp"] for i in r["items"]] == [e["timestamp"] for e in reversed(entries)]
    assert r["items"][0]["id"] == entries[3]["timestamp"]
    assert "id" not in entries[3]                           # input not mutated

    assert A.recent(entries, q="ALPHA")["total"] == 2
    assert A.recent(entries, status="empty")["total"] == 1
    assert A.recent(entries, app="Slack")["items"][0]["text"] == "Alpha two"
    assert A.recent(entries, q="alpha", app="Code")["total"] == 1

    page = A.recent(entries, limit=2, offset=2)
    assert page["total"] == 4
    assert [i["text"] for i in page["items"]] == ["Alpha two", "alpha one"]
    assert A.recent(entries, limit=2, offset=10)["items"] == []


# -------------------------------------------------------------- dashboard ---


def test_build_dashboard_shape_and_serialisable():
    entries = [mk(at(d)) for d in (0, 1, 8, 40)] + [mk(at(0), status="hallucination")]
    d = A.build_dashboard(entries, days=30, typing_wpm=40.0, now=NOW)
    assert set(d) == {"window", "totals", "daily", "weekly", "heatmap", "wpm", "latency",
                      "outcomes", "apps", "lengths", "streaks", "terms", "models",
                      "backends", "generated_at"}
    assert d["window"] == {"days": 30,
                           "from": datetime(2026, 8, 14, 0, 0, tzinfo=TZ).isoformat(),
                           "to": NOW.isoformat()}
    assert d["totals"]["dictations"] == 4                  # the 40-day-old entry is out
    assert len(d["daily"]) == 30
    assert len(d["weekly"]) == 12
    json.dumps(d)

    all_time = A.build_dashboard(entries, days=None, now=NOW)
    assert all_time["window"]["from"] == at(40).isoformat()
    assert all_time["totals"]["dictations"] == 5
    json.dumps(all_time)

    empty = A.build_dashboard([], days=7, now=NOW)
    assert empty["totals"]["dictations"] == 0 and len(empty["daily"]) == 7
    json.dumps(empty)


def test_nothing_raises_on_garbage():
    junk = [None, 1, "x", {}, {"timestamp": 5}, {"timestamp": "2026-09-12T10:00:00+02:00",
                                                  "status": "ok", "words": "ten",
                                                  "audio_seconds": "long", "wpm": True}]
    json.dumps(A.build_dashboard(junk, days=7, now=NOW))
    json.dumps(A.recent(junk))


# ------------------------------------------------------------ stats.load ---


def test_load_entries_tolerant(tmp_path):
    p = tmp_path / "history.jsonl"
    good = mk(at(0))
    p.write_text(
        json.dumps(good) + "\n"
        + "\n"
        + '{"timestamp": "2026-09-12T10:0'            # truncated trailing write
        + "\n[1, 2]\n"
        + json.dumps({"timestamp": "nope", "status": "ok"}) + "\n",
        encoding="utf-8",
    )
    entries, skipped = stats.load_entries(p)
    assert len(entries) == 2 and skipped == 2              # no cutoff: bad timestamp kept
    entries, skipped = stats.load_entries(p, days=7)
    assert len(entries) == 1 and skipped == 3              # with cutoff it cannot be placed
    assert entries[0] == good
    assert "id" not in entries[0] and isinstance(entries[0]["timestamp"], str)


def test_load_wrapper_prints_skipped(tmp_path, capsys):
    p = tmp_path / "history.jsonl"
    p.write_text(json.dumps(mk(at(0))) + "\n{broken\n", encoding="utf-8")
    out = stats.load(p)
    assert len(out) == 1
    assert "skipped 1 unreadable" in capsys.readouterr().out


# ------------------------------------------------------------- real data ---

REAL = Path.home() / "Library" / "Application Support" / "WhisperLocal" / "history.jsonl"


@pytest.mark.skipif(not REAL.exists(), reason="no real history on this machine")
def test_build_dashboard_on_real_history():
    entries, _skipped = stats.load_entries(REAL)
    assert entries
    for days in (None, 7, 30):
        d = A.build_dashboard(entries, days=days)
        json.dumps(d)
        assert d["totals"]["dictations"] >= d["totals"]["ok"]
        assert d["outcomes"]["total"] == d["totals"]["dictations"]
    r = A.recent(entries, limit=5)
    json.dumps(r)
    assert len(r["items"]) <= 5
