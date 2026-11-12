from __future__ import annotations

import datetime as dt
import math
import statistics
from collections import Counter

from .models import Slot
from .notify import Message
from .state import WATCH_GAP_S, BotState

DIGEST_HOUR = 8
GAPS_LISTED = 5
RULES = (
    ("skipped", "already had a job then"),
    ("too close", "too close to another job"),
    ("day full", "the day was already full"),
    ("too soon", "started too soon"),
    ("day off", "marked as a day off"),
)
OTHERS = (
    ("refused", "Elisa refused the Accept"),
    ("error", "Accept couldn't be tried"),
    ("test_mode", "Found in test mode, not clicked"),
)
OPENING_OUTCOMES = frozenset({"applied", "unverified", "missed", "gone", *(key for key, _ in RULES + OTHERS)})


def report_week(now: dt.datetime) -> dt.date:
    monday = now.date() - dt.timedelta(days=now.weekday())
    due = dt.datetime.combine(monday, dt.time(DIGEST_HOUR), tzinfo=now.tzinfo)
    return monday - dt.timedelta(days=7 if now >= due else 14)


def weekly_digest(state: BotState, start: dt.date, end: dt.date, now: dt.datetime) -> Message:
    first, last = start.isoformat(), end.isoformat()
    events = [e for e in state.activity if isinstance(e, dict) and first <= str(e.get("at"))[:10] < last]
    span = f"{_day(start)} to {_day(end - dt.timedelta(days=1))}"
    so_far = " (so far)" if now < _midnight(end, now) else ""
    lines = [f"Week of {span}{so_far}.", ""]
    lines += _openings(events)
    lines += _speed(events)
    lines += _on_calendar(state, first, last)
    lines += _watching(state, start, end, now)
    return Message(f"Elisa: weekly report, {span}", "\n".join(lines).rstrip() + "\n", sms=False)


def _openings(events: list[dict]) -> list[str]:
    counts = Counter(str(e.get("outcome")) for e in events)
    seen = sum(counts[outcome] for outcome in OPENING_OUTCOMES)
    if not seen:
        return ["Openings the bot saw: none.", ""]
    accepted = _slots(events, "applied")
    lines = [f"Openings the bot saw: {seen}", f"- Accepted: {counts['applied']}" + (f" ({_labels(accepted)})" if accepted else "")]
    if counts["unverified"]:
        lines.append(f'- Accept clicked but not confirmed ("please check"): {counts["unverified"]}')
    lines.append(f"- Taken by someone else first: {counts['missed'] + counts['gone']}")
    lines.append(f"- Not accepted because of the rules: {sum(counts[key] for key, _ in RULES)}")
    for key, wording in RULES:
        if counts[key]:
            slots = _slots(events, key)
            lines.append(f"    {wording}: {counts[key]}" + (f" ({_labels(slots)})" if slots else ""))
    for key, wording in OTHERS:
        if counts[key]:
            lines.append(f"- {wording}: {counts[key]}")
    return [*lines, ""]


def _speed(events: list[dict]) -> list[str]:
    times = [
        e["seconds"]
        for e in events
        if e.get("outcome") == "applied" and isinstance(e.get("seconds"), (int, float)) and not isinstance(e.get("seconds"), bool)
    ]
    if not times:
        return []
    return [f"Speed: accepted a median of {statistics.median(times):.1f} s after the bot noticed the opening (fastest {min(times):.1f} s).", ""]


def _on_calendar(state: BotState, first: str, last: str) -> list[str]:
    there = _total(state, first, last, "on_calendar")
    missing = _total(state, first, last, "not_on_calendar")
    if not there + missing:
        return []
    return [f"Checked on the calendar after Accept: {there} there, {missing} not there.", ""]


def _watching(state: BotState, start: dt.date, end: dt.date, now: dt.datetime) -> list[str]:
    watch = state.watch if isinstance(state.watch, dict) else {}
    begin, finish = _midnight(start, now), min(_midnight(end, now), now)
    since = _moment(watch.get("since"), now)
    lines = []
    if since is None or max(begin, since) >= finish:
        lines.append("Watching: not measured yet.")
    else:
        measured = max(begin, since)
        raw = watch.get("gaps") if isinstance(watch.get("gaps"), list) else []
        gaps = [(_moment(g[0], now), _moment(g[1], now)) for g in raw if isinstance(g, list) and len(g) == 2]
        last_ok = _moment(watch.get("last_ok"), now)
        if last_ok is not None and (now - last_ok).total_seconds() > WATCH_GAP_S:
            gaps.append((last_ok, now))
        clipped = sorted(
            (max(a, measured), min(b, finish)) for a, b in gaps if a is not None and b is not None and b > measured and a < finish
        )
        missing = sum((b - a).total_seconds() for a, b in clipped)
        share = max(0.0, 1 - missing / (finish - measured).total_seconds())
        where = "of the week" if measured == begin else f"since {_when(measured)}, when this report began measuring"
        lines.append(f"Watching: {_percent(share)} {where}.")
        if clipped:
            lines.append("Gaps of 10 minutes or more:")
            lines += [f"- {_span(a, b)}" for a, b in clipped[:GAPS_LISTED]]
            if len(clipped) > GAPS_LISTED:
                lines.append(f"- and {len(clipped) - GAPS_LISTED} more")
    reads, failed = _total(state, start.isoformat(), end.isoformat(), "reads"), _total(state, start.isoformat(), end.isoformat(), "failed")
    if reads or failed:
        lines.append(f"Calendar reads: {reads:,}" + (f" (another {failed:,} failed and were retried)." if failed else "."))
    return lines


def _total(state: BotState, first: str, last: str, key: str) -> int:
    return sum(
        value[key]
        for day, value in state.daily.items()
        if first <= day < last and isinstance(value, dict) and type(value.get(key)) is int
    )


def _slots(events: list[dict], outcome: str) -> list[Slot]:
    found = []
    for event in events:
        if event.get("outcome") == outcome:
            try:
                found.append(Slot.parse(str(event.get("slot"))))
            except ValueError:
                continue
    return sorted(found)


def _labels(slots: list[Slot]) -> str:
    return ", ".join(slot.label() for slot in slots)


def _percent(share: float) -> str:
    return f"{math.floor(share * 1000) / 10:g}%"


def _midnight(day: dt.date, now: dt.datetime) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(), tzinfo=now.tzinfo)


def _moment(text: object, now: dt.datetime) -> dt.datetime | None:
    if not isinstance(text, str):
        return None
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment.astimezone(now.tzinfo) if moment.tzinfo is not None else None


def _day(day: dt.date) -> str:
    return f"{day:%a %b} {day.day}"


def _clock(moment: dt.datetime) -> str:
    return f"{moment:%I:%M %p}".lstrip("0")


def _when(moment: dt.datetime) -> str:
    return f"{_day(moment.date())}, {_clock(moment)}"


def _span(a: dt.datetime, b: dt.datetime) -> str:
    if a.date() == b.date():
        return f"{_when(a)} to {_clock(b)}"
    return f"{_when(a)} to {_when(b)}"
