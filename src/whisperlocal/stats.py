"""
WhisperLocal — dictation history stats.

Summarises the JSONL history file written by HistoryLog.

    whisperlocal stats              # all time
    whisperlocal stats --days 7     # last 7 days
    whisperlocal stats --text       # also dump the transcripts

The file is JSONL — one JSON object per line — so anything this does not show
is a one-liner elsewhere:

    jq -r 'select(.status=="ok") | .text' history.jsonl
    pandas.read_json("history.jsonl", lines=True)
"""

from __future__ import annotations

import datetime
import json
from collections import Counter, defaultdict
from pathlib import Path

from whisperlocal.config import Settings


def load(path: Path, days: int | None = None) -> list[dict]:
    """Read the log, skipping any malformed line rather than dying on it."""
    entries: list[dict] = []
    skipped = 0

    cutoff = None
    if days:
        cutoff = datetime.datetime.now().astimezone() - datetime.timedelta(days=days)

    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if cutoff:
                    ts = datetime.datetime.fromisoformat(entry["timestamp"])
                    if ts < cutoff:
                        continue
                entries.append(entry)
            except Exception:
                # A partial trailing line is the expected cost of append-only
                # writes if the app died mid-write. Never fatal.
                skipped += 1

    if skipped:
        print(f"(skipped {skipped} unreadable line(s))\n")
    return entries


def bar(n: float, total: float, width: int = 24) -> str:
    filled = int(width * n / total) if total else 0
    return "█" * filled + "·" * (width - filled)


def report(settings: Settings, days: int | None = None, show_text: bool = False,
           path: Path | None = None) -> int:
    """Print the summary. Returns a process exit code."""
    path = path or settings.history_path

    if not path.exists():
        print(f"No history yet at {path}")
        if not settings.history_enabled:
            print("History is switched off — set history_enabled = true to collect it.")
        else:
            print("Dictate something first, then run this again.")
        return 0

    entries = load(path, days)
    if not entries:
        print("No entries in that window.")
        return 0

    ok = [e for e in entries if e.get("status") == "ok"]
    scope = f"last {days} days" if days else "all time"

    print(f"WhisperLocal — dictation stats ({scope})")
    print("=" * 52)
    print(f"  dictations      {len(entries)}  ({len(ok)} produced text)")

    if ok:
        words = sum(e.get("words") or 0 for e in ok)
        secs = sum(e.get("audio_seconds") or 0 for e in ok)
        wpms = [e["wpm"] for e in ok if e.get("wpm")]
        lat = [e["transcribe_ms"] for e in ok if e.get("transcribe_ms")]
        print(f"  words dictated  {words:,}")
        print(f"  time speaking   {secs / 60:.1f} min")
        if wpms:
            print(f"  speaking rate   {sum(wpms) / len(wpms):.0f} wpm avg")
        if lat:
            lat.sort()
            print(
                f"  transcribe time {sum(lat) / len(lat):.0f} ms avg, "
                f"{lat[len(lat) // 2]:.0f} ms median, {lat[-1]:.0f} ms worst"
            )

    # Outcomes — the failure rates are the reason failures get logged at all.
    print("\n  outcomes")
    counts = Counter(e.get("status") for e in entries)
    for status, n in counts.most_common():
        print(f"    {str(status):15} {n:5}  {n / len(entries) * 100:5.1f}%  {bar(n, len(entries))}")

    halluc = counts.get("hallucination", 0)
    if halluc:
        print(
            f"\n    {halluc} hallucinated chunk(s) caught and discarded "
            f"({halluc / len(entries) * 100:.1f}% of attempts)"
        )

    # Where the text went
    apps = Counter(e.get("app") or "unknown" for e in entries)
    print("\n  by app")
    for app, n in apps.most_common(10):
        print(f"    {app[:22]:22} {n:5}  {bar(n, len(entries))}")

    # Daily volume
    per_day: dict[str, int] = defaultdict(int)
    for e in ok:
        per_day[e["timestamp"][:10]] += e.get("words") or 0
    if per_day:
        print("\n  words per day")
        peak = max(per_day.values())
        for day in sorted(per_day)[-14:]:
            print(f"    {day}  {per_day[day]:6,}  {bar(per_day[day], peak)}")

    if show_text:
        stored = [e for e in ok if e.get("text")]
        print("\n  transcripts")
        print("  " + "-" * 50)
        if not stored:
            print("  (none stored — history_text is off)")
        for e in stored:
            print(f"  [{e['timestamp']}] ({e.get('app') or '?'})")
            print(f"    {e['text']}\n")

    return 0
