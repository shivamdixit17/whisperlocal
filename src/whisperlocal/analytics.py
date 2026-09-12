"""
WhisperLocal — pure analytics over the dictation history.

Every function here takes a list of history entries (the dicts written by
HistoryLog, one per line of history.jsonl) and returns plain JSON-serialisable
data: dicts, lists, strings, ints, floats, None. No datetime objects, no tuple
keys, nothing a frontend has to unpick.

Design rules:

* stdlib only — this module must import on Linux CI with nothing installed.
* never raise on a malformed entry; skip it and carry on. The history file is
  append-only and the app may die mid-write, so a garbage line is expected.
* timestamps are interpreted in the offset they were recorded with. A
  dictation at 10:00 local time lands in the 10:00 heatmap cell regardless of
  where the machine is now.
* `days` is calendar days: days=1 is "today", days=7 is today plus the six
  days before it. That is what a dashboard's "7d" button means.
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Iterable

STATUSES = ("ok", "empty", "hallucination", "too_short", "no_audio", "backend_error")
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
LENGTH_EDGES = [0, 2, 5, 10, 20, 40, 60]

STOPWORDS = frozenset(
    """
    a about above after again against all also am an and any are aren't as at
    be because been before being below between both but by can can't cannot
    could couldn't did didn't do does doesn't doing don't down during each few
    for from further get got had hadn't has hasn't have haven't having he he'd
    he'll he's her here here's hers herself him himself his how how's i i'd
    i'll i'm i've if in into is isn't it it's its itself just let's like me
    more most mustn't my myself no nor not of off on once only or other ought
    our ours ourselves out over own really same shan't she she'd she'll she's
    should shouldn't so some such than that that's the their theirs them
    themselves then there there's these they they'd they'll they're they've
    this those through to too under until up us very was wasn't we we'd we'll
    we're we've were weren't what what's when when's where where's which while
    who who's whom why why's will with won't would wouldn't yeah yes you you'd
    you'll you're you've your yours yourself yourselves okay ok please thing
    things something anything nothing know think want need make made going
    go went way one two also still even much many well back much
    """.split()
)

_TOKEN_RE = re.compile(r"[^\W_]+(?:'[^\W_]+)*", re.UNICODE)
_NUMERIC_RE = re.compile(r"^[\d.,]+$")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    """A real number or None. Bools are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return float(value)


def _r1(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _pct(part: float, whole: float) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now().astimezone()
    if now.tzinfo is None:
        return now.astimezone()
    return now


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def entry_time(entry: Any) -> datetime | None:
    """Parse the entry's timestamp; None if the entry or timestamp is bad."""
    if not isinstance(entry, dict):
        return None
    ts = entry.get("timestamp")
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _timed(entries: Iterable[Any]) -> list[tuple[dict, datetime]]:
    """(entry, parsed time) for every entry that is a dict with a valid time."""
    out = []
    for e in entries:
        ts = entry_time(e)
        if ts is not None:
            out.append((e, ts))
    return out


def _is_ok(entry: dict) -> bool:
    return entry.get("status") == "ok"


_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def _app_name(entry: dict) -> str:
    """The app name as a dashboard should show it; "unknown" when missing.

    macOS hands some names over with a leading left-to-right mark
    ("\u200eWhatsApp"); strip those so grouping and filtering agree.
    """
    app = entry.get("app")
    if not isinstance(app, str):
        return "unknown"
    app = _INVISIBLE_RE.sub("", app).strip()
    return app or "unknown"


def short_model(model: Any) -> str:
    """'mlx-community/whisper-base-mlx' -> 'whisper-base'."""
    if not isinstance(model, str) or not model:
        return "unknown"
    name = model
    if name.startswith("mlx-community/"):
        name = name[len("mlx-community/"):]
    if name.endswith("-mlx"):
        name = name[: -len("-mlx")]
    return name or "unknown"


def percentile(values: list[float], p: float) -> float | None:
    """Linear-interpolated percentile (0..100). None for an empty list."""
    vals = sorted(v for v in (_num(x) for x in values) if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    p = min(max(p, 0.0), 100.0)
    pos = (len(vals) - 1) * p / 100.0
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


# --------------------------------------------------------------------------
# window
# --------------------------------------------------------------------------


def window_start(days: int | None, now: datetime | None = None) -> date | None:
    """First calendar day of the window, or None for all time."""
    if not days or days <= 0:
        return None
    return _now(now).date() - timedelta(days=days - 1)


def filter_window(entries: list[dict], days: int | None,
                  now: datetime | None = None) -> list[dict]:
    """Entries whose (own-offset) calendar day falls in the last `days` days.

    days=None returns every entry that has a parseable timestamp.
    """
    start = window_start(days, now)
    out = []
    for e, ts in _timed(entries):
        if start is None or ts.date() >= start:
            out.append(e)
    return out


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------


def totals(entries: list[dict], typing_wpm: float = 40.0) -> dict:
    entries = [e for e in entries if isinstance(e, dict)]
    ok = [e for e in entries if _is_ok(e)]
    words = sum(int(_num(e.get("words")) or 0) for e in ok)
    chars = sum(int(_num(e.get("chars")) or 0) for e in ok)
    secs = sum(_num(e.get("audio_seconds")) or 0.0 for e in ok)
    ms = sum(_num(e.get("transcribe_ms")) or 0.0 for e in ok)
    wpms = [w for w in (_num(e.get("wpm")) for e in ok) if w]

    typing_minutes = words / typing_wpm if typing_wpm and typing_wpm > 0 else 0.0
    spent_minutes = (secs + ms / 1000.0) / 60.0

    return {
        "dictations": len(entries),
        "ok": len(ok),
        "words": words,
        "chars": chars,
        "minutes_speaking": _r1(secs / 60.0),
        "avg_words": _r1(words / len(ok)) if ok else None,
        "avg_seconds": _r1(secs / len(ok)) if ok else None,
        "avg_wpm": _r1(sum(wpms) / len(wpms)) if wpms else None,
        "minutes_saved": _r1(typing_minutes - spent_minutes),
        "typing_wpm": typing_wpm,
    }


def daily_series(entries: list[dict], days: int | None,
                 now: datetime | None = None) -> list[dict]:
    """One row per calendar day, zero-filled, oldest first."""
    today = _now(now).date()
    timed = _timed(entries)

    start = window_start(days, now)
    if start is None:
        if not timed:
            return []
        start = min(ts.date() for _, ts in timed)
    end = max([today] + [ts.date() for _, ts in timed if ts.date() > today])

    rows: dict[date, dict] = {}
    d = start
    while d <= end:
        rows[d] = {"date": d.isoformat(), "dictations": 0, "ok": 0, "words": 0, "minutes": 0.0}
        d += timedelta(days=1)

    for e, ts in timed:
        row = rows.get(ts.date())
        if row is None:
            continue
        row["dictations"] += 1
        if _is_ok(e):
            row["ok"] += 1
            row["words"] += int(_num(e.get("words")) or 0)
            row["minutes"] += (_num(e.get("audio_seconds")) or 0.0) / 60.0

    out = list(rows.values())
    for row in out:
        row["minutes"] = round(row["minutes"], 1)
    return out


def _weekly_buckets(entries: list[dict], weeks: int | None,
                    now: datetime | None) -> dict[date, list[tuple[dict, datetime]]]:
    """Entries grouped by ISO week start. With `weeks`, zero-filled for the
    last `weeks` weeks ending in the current one and anything older dropped."""
    timed = _timed(entries)
    buckets: dict[date, list] = defaultdict(list)
    if weeks and weeks > 0:
        this_week = _week_start(_now(now).date())
        for i in range(weeks - 1, -1, -1):
            buckets[this_week - timedelta(weeks=i)] = []
        first = this_week - timedelta(weeks=weeks - 1)
        for e, ts in timed:
            ws = _week_start(ts.date())
            if first <= ws <= this_week:
                buckets[ws].append((e, ts))
    else:
        for e, ts in timed:
            buckets[_week_start(ts.date())].append((e, ts))
    return dict(sorted(buckets.items()))


def weekly_series(entries: list[dict], weeks: int = 12,
                  now: datetime | None = None) -> list[dict]:
    """ISO weeks (Monday start), zero-filled for the last `weeks` weeks."""
    out = []
    prev_words: int | None = None
    for ws, items in _weekly_buckets(entries, weeks, now).items():
        ok = [e for e, _ in items if _is_ok(e)]
        words = sum(int(_num(e.get("words")) or 0) for e in ok)
        minutes = sum(_num(e.get("audio_seconds")) or 0.0 for e in ok) / 60.0
        delta = None
        if prev_words is not None and prev_words > 0:
            delta = round((words - prev_words) / prev_words * 100, 1)
        out.append({
            "week_start": ws.isoformat(),
            "dictations": len(items),
            "ok": len(ok),
            "words": words,
            "minutes": round(minutes, 1),
            "delta_pct": delta,
        })
        prev_words = words
    return out


def hour_weekday_heatmap(entries: list[dict]) -> dict:
    counts = [[0] * 24 for _ in range(7)]
    for _, ts in _timed(entries):
        counts[ts.weekday()][ts.hour] += 1
    return {
        "rows": list(WEEKDAYS),
        "cols": list(range(24)),
        "counts": counts,
        "max": max(max(r) for r in counts),
    }


def wpm_distribution(entries: list[dict], bin_size: int = 20) -> dict:
    bin_size = int(bin_size) if bin_size and bin_size > 0 else 20
    timed = [(e, ts) for e, ts in _timed(entries) if _is_ok(e)]
    values = []
    per_week: dict[date, list[float]] = defaultdict(list)
    for e, ts in timed:
        w = _num(e.get("wpm"))
        if w is None or w <= 0:
            continue
        values.append(w)
        per_week[_week_start(ts.date())].append(w)

    if not values:
        return {"bins": [], "p50": None, "p90": None, "mean": None, "trend": []}

    top = max(values)
    n_bins = int(top // bin_size) + 1
    hist = [0] * n_bins
    for w in values:
        hist[min(int(w // bin_size), n_bins - 1)] += 1
    bins = [
        {"from": i * bin_size, "to": (i + 1) * bin_size, "count": c}
        for i, c in enumerate(hist)
    ]
    trend = [
        {"week_start": ws.isoformat(), "median_wpm": round(statistics.median(v), 1), "n": len(v)}
        for ws, v in sorted(per_week.items())
    ]
    return {
        "bins": bins,
        "p50": _r1(percentile(values, 50)),
        "p90": _r1(percentile(values, 90)),
        "mean": _r1(sum(values) / len(values)),
        "n": len(values),
        "trend": trend,
    }


def latency_by_model(entries: list[dict]) -> list[dict]:
    per_model: dict[str, list[float]] = defaultdict(list)
    for e in entries:
        if not isinstance(e, dict):
            continue
        ms = _num(e.get("transcribe_ms"))
        if ms is None or ms < 0:
            continue
        model = e.get("model")
        per_model[model if isinstance(model, str) and model else "unknown"].append(ms)

    out = []
    for model, ms in per_model.items():
        out.append({
            "model": model,
            "short": short_model(model),
            "n": len(ms),
            "p50_ms": int(round(percentile(ms, 50) or 0)),
            "p95_ms": int(round(percentile(ms, 95) or 0)),
            "max_ms": int(round(max(ms))),
            "mean_ms": int(round(sum(ms) / len(ms))),
        })
    out.sort(key=lambda r: (-r["n"], r["model"]))
    return out


def _status_of(entry: dict) -> str:
    s = entry.get("status")
    return s if isinstance(s, str) and s else "unknown"


def outcomes(entries: list[dict]) -> dict:
    entries = [e for e in entries if isinstance(e, dict)]
    counts = Counter(_status_of(e) for e in entries)
    total = len(entries)

    trend = []
    for ws, items in _weekly_buckets(entries, None, None).items():
        c = Counter(_status_of(e) for e, _ in items)
        n = len(items)
        trend.append({
            "week_start": ws.isoformat(),
            "total": n,
            "ok_pct": _pct(c.get("ok", 0), n),
            "hallucination_pct": _pct(c.get("hallucination", 0), n),
            "too_short_pct": _pct(c.get("too_short", 0), n),
            "empty_pct": _pct(c.get("empty", 0), n),
        })

    ordered = {s: counts[s] for s in STATUSES if s in counts}
    for s, n in counts.most_common():
        ordered.setdefault(s, n)
    return {
        "counts": ordered,
        "total": total,
        "rates": {s: _pct(n, total) for s, n in ordered.items()},
        "trend": trend,
    }


def by_app(entries: list[dict], limit: int = 10) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        if not isinstance(e, dict):
            continue
        groups[_app_name(e)].append(e)

    out = []
    for app, items in groups.items():
        ok = [e for e in items if _is_ok(e)]
        n = len(items)
        bundles = Counter(
            b for b in (e.get("app_bundle_id") for e in items) if isinstance(b, str) and b
        )
        out.append({
            "app": app,
            "bundle_id": bundles.most_common(1)[0][0] if bundles else None,
            "dictations": n,
            "ok": len(ok),
            "words": sum(int(_num(e.get("words")) or 0) for e in ok),
            "minutes": round(sum(_num(e.get("audio_seconds")) or 0.0 for e in ok) / 60.0, 1),
            "hallucination_pct": _pct(sum(1 for e in items if e.get("status") == "hallucination"), n),
            "too_short_pct": _pct(sum(1 for e in items if e.get("status") == "too_short"), n),
        })
    out.sort(key=lambda r: (-r["dictations"], r["app"].lower()))
    return out[:limit] if limit and limit > 0 else out


def length_histogram(entries: list[dict]) -> dict:
    edges = LENGTH_EDGES
    buckets = []
    for i, lo in enumerate(edges):
        hi = edges[i + 1] if i + 1 < len(edges) else None
        label = f"{lo}-{hi}s" if hi is not None else f"{lo}s+"
        buckets.append({"label": label, "from": lo, "to": hi, "count": 0})

    for e in entries:
        if not isinstance(e, dict):
            continue
        s = _num(e.get("audio_seconds"))
        if s is None or s < 0:
            continue
        idx = len(edges) - 1
        for i in range(len(edges) - 1):
            if edges[i] <= s < edges[i + 1]:
                idx = i
                break
        buckets[idx]["count"] += 1
    return {"buckets": buckets}


def streaks(entries: list[dict], now: datetime | None = None) -> dict:
    today = _now(now).date()
    active = sorted({ts.date() for e, ts in _timed(entries) if _is_ok(e)})
    if not active:
        return {"current_days": 0, "longest_days": 0, "active_days": 0, "last_active": None}

    active_set = set(active)
    longest = run = 1
    for prev, cur in zip(active, active[1:]):
        run = run + 1 if cur - prev == timedelta(days=1) else 1
        longest = max(longest, run)

    anchor = None
    for candidate in (today, today - timedelta(days=1)):
        if candidate in active_set:
            anchor = candidate
            break
    current = 0
    while anchor is not None and anchor in active_set:
        current += 1
        anchor -= timedelta(days=1)

    return {
        "current_days": current,
        "longest_days": longest,
        "active_days": len(active),
        "last_active": active[-1].isoformat(),
    }


def _tokens(text: str) -> list[str]:
    return [t.strip("'") for t in _TOKEN_RE.findall(text.lower())]


def _keep_term(tok: str, min_len: int) -> bool:
    return len(tok) >= min_len and tok not in STOPWORDS and not _NUMERIC_RE.match(tok)


def top_terms(entries: list[dict], n: int = 30, min_len: int = 3) -> dict:
    unigrams: Counter = Counter()
    bigrams: Counter = Counter()
    has_text = False
    for e in entries:
        if not isinstance(e, dict) or not _is_ok(e):
            continue
        text = e.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        has_text = True
        toks = [t for t in _tokens(text) if t]
        keep = [_keep_term(t, min_len) for t in toks]
        for t, k in zip(toks, keep):
            if k:
                unigrams[t] += 1
        for i in range(len(toks) - 1):
            if keep[i] and keep[i + 1]:
                bigrams[f"{toks[i]} {toks[i + 1]}"] += 1

    def top(counter: Counter) -> list[dict]:
        items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
        return [{"term": t, "count": c} for t, c in items[:n]]

    return {"unigrams": top(unigrams), "bigrams": top(bigrams), "has_text": has_text}


def model_usage(entries: list[dict]) -> list[dict]:
    counts: Counter = Counter()
    for e, ts in _timed(entries):
        counts[(_week_start(ts.date()).isoformat(), short_model(e.get("model")))] += 1
    rows = [
        {"week_start": ws, "model_short": m, "dictations": c}
        for (ws, m), c in counts.items()
    ]
    rows.sort(key=lambda r: (r["week_start"], -r["dictations"], r["model_short"]))
    return rows


def backend_usage(entries: list[dict]) -> dict:
    out: dict[str, dict] = {
        "local": {"dictations": 0, "audio_seconds": 0.0},
        "api": {"dictations": 0, "audio_seconds": 0.0},
    }
    for e in entries:
        if not isinstance(e, dict):
            continue
        b = e.get("backend")
        key = b if isinstance(b, str) and b else "local"
        row = out.setdefault(key, {"dictations": 0, "audio_seconds": 0.0})
        row["dictations"] += 1
        row["audio_seconds"] += _num(e.get("audio_seconds")) or 0.0
    for row in out.values():
        row["audio_seconds"] = round(row["audio_seconds"], 1)
    return out


def recent(entries: list[dict], *, limit: int = 50, offset: int = 0,
           q: str | None = None, status: str | None = None,
           app: str | None = None) -> dict:
    """Newest first, filtered and paged. Items are copies with an "id"."""
    timed = _timed(entries)
    has_text = any(isinstance(e.get("text"), str) and e["text"] for e, _ in timed)

    needle = q.lower() if isinstance(q, str) and q.strip() else None
    rows = []
    for e, ts in timed:
        if status and e.get("status") != status:
            continue
        if app and _app_name(e) != app:
            continue
        if needle is not None:
            text = e.get("text")
            if not isinstance(text, str) or needle not in text.lower():
                continue
        rows.append((ts, e))
    rows.sort(key=lambda r: r[0], reverse=True)

    offset = max(int(offset or 0), 0)
    limit = max(int(limit or 0), 0)
    page = rows[offset: offset + limit] if limit else rows[offset:]
    items = [{**e, "id": e.get("timestamp")} for _, e in page]
    return {"total": len(rows), "items": items, "has_text": has_text}


# --------------------------------------------------------------------------
# the whole thing
# --------------------------------------------------------------------------


def build_dashboard(entries: list[dict], *, days: int | None,
                    typing_wpm: float = 40.0, now: datetime | None = None) -> dict:
    now = _now(now)
    windowed = filter_window(entries, days, now)

    start = window_start(days, now)
    if start is not None:
        window_from = datetime.combine(start, datetime.min.time(), tzinfo=now.tzinfo).isoformat()
    else:
        times = [ts for _, ts in _timed(windowed)]
        window_from = min(times).isoformat() if times else None

    return {
        "window": {"days": days, "from": window_from, "to": now.isoformat()},
        "totals": totals(windowed, typing_wpm),
        "daily": daily_series(windowed, days, now),
        "weekly": weekly_series(windowed, 12, now),
        "heatmap": hour_weekday_heatmap(windowed),
        "wpm": wpm_distribution(windowed),
        "latency": latency_by_model(windowed),
        "outcomes": outcomes(windowed),
        "apps": by_app(windowed),
        "lengths": length_histogram(windowed),
        "streaks": streaks(windowed, now),
        "terms": top_terms(windowed),
        "models": model_usage(windowed),
        "backends": backend_usage(windowed),
        "generated_at": now.isoformat(),
    }
