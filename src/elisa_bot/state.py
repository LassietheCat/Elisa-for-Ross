from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from .config import Settings
from .models import Half, Slot
from .timeutil import to_iso

log = logging.getLogger(__name__)

STATE_VERSION = 1
ACTIVITY_MAX = 2000
HISTORY_DAYS = 15
WATCH_GAP_S = 600
MAX_DAYS_OFF = 366
MAX_JOBS_PER_DAY = 4
SERVICE_TYPES = ("VRI", "OPI", "OSI")


@dataclass
class BotState:
    version: int = STATE_VERSION
    baseline_done: bool = False
    baseline_pending: list[str] = field(default_factory=list)
    watched_through: str | None = None
    seen: list[str] = field(default_factory=list)
    applied: dict[str, str] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)
    applies_by_day: dict[str, int] = field(default_factory=dict)
    last_run_at: str | None = None
    last_run_ok: bool | None = None
    last_error: str | None = None
    activity: list[dict] = field(default_factory=list)
    daily: dict[str, dict[str, int]] = field(default_factory=dict)
    jobs: dict[str, dict] = field(default_factory=dict)
    landing: dict[str, dict] = field(default_factory=dict)
    watch: dict = field(default_factory=dict)

    def count(self, day: dt.date, key: str, amount: int = 1) -> None:
        if amount <= 0:
            return
        entry = self.daily.setdefault(day.isoformat(), {})
        entry[key] = entry.get(key, 0) + amount

    def add_activity(self, at: str, slot: str | None, outcome: str, seconds: float | None) -> None:
        self.activity = [*self.activity, {"at": at, "slot": slot, "outcome": outcome, "seconds": seconds}][-ACTIVITY_MAX:]

    def watched(self, now: dt.datetime) -> None:
        watch = self.watch if isinstance(self.watch, dict) else {}
        raw = watch.get("gaps")
        gaps = [gap for gap in raw if isinstance(gap, list) and len(gap) == 2] if isinstance(raw, list) else []
        last = watch.get("last_ok")
        try:
            if isinstance(last, str) and (now - dt.datetime.fromisoformat(last)).total_seconds() > WATCH_GAP_S:
                gaps.append([last, to_iso(now)])
        except (TypeError, ValueError):
            pass
        since = watch.get("since")
        self.watch = {"since": since if isinstance(since, str) else to_iso(now), "last_ok": to_iso(now), "gaps": gaps}

    def seen_slots(self) -> set[Slot]:
        return {Slot.parse(text) for text in self.seen}

    def applied_slots(self) -> set[Slot]:
        return {Slot.parse(text) for text in self.applied}

    def applies_on(self, day: dt.date) -> int:
        return int(self.applies_by_day.get(day.isoformat(), 0))

    def record_apply(self, slot: Slot, now: dt.datetime) -> None:
        self.applied[str(slot)] = to_iso(now)
        day = now.date().isoformat()
        self.applies_by_day[day] = self.applies_by_day.get(day, 0) + 1

    def expect_on_calendar(self, slot: Slot, jobs_before: int, now: dt.datetime, *, unsure: bool) -> None:
        entry = self.landing.get(slot.date.isoformat())
        if not isinstance(entry, dict) or type(entry.get("expect")) is not int:
            entry = {"expect": jobs_before}
        slots = entry.get("slots")
        self.landing[slot.date.isoformat()] = {
            "expect": entry["expect"] + 1,
            "slots": [*(slots if isinstance(slots, list) else []), str(slot)],
            "unsure": entry.get("unsure") is True or unsure,
            "at": to_iso(now),
        }

    def prune(self, today: dt.date, keep_days: int = 7) -> None:
        cutoff = today - dt.timedelta(days=keep_days)
        self.applied = {k: v for k, v in self.applied.items() if Slot.parse(k).date >= cutoff}
        self.attempts = {k: v for k, v in self.attempts.items() if Slot.parse(k).date >= cutoff}
        self.applies_by_day = {
            k: v for k, v in self.applies_by_day.items() if dt.date.fromisoformat(k) >= cutoff
        }
        history = (today - dt.timedelta(days=HISTORY_DAYS)).isoformat()
        self.daily = {k: v for k, v in self.daily.items() if k >= history}
        self.activity = [e for e in self.activity if str(e.get("at"))[:10] >= history][-ACTIVITY_MAX:]
        if isinstance(self.watch, dict) and isinstance(self.watch.get("gaps"), list):
            self.watch["gaps"] = [g for g in self.watch["gaps"] if isinstance(g, list) and len(g) == 2 and str(g[1])[:10] >= history]
        self.jobs = {k: v for k, v in self.jobs.items() if k >= f"{today:%Y-%m}"}
        self.landing = {k: v for k, v in self.landing.items() if k >= today.isoformat()}


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> BotState:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return BotState()
        except (json.JSONDecodeError, UnicodeDecodeError):
            backup = self.path.with_name(self.path.name + ".corrupt")
            os.replace(self.path, backup)
            log.warning("%s was unreadable; moved it to %s and started fresh", self.path.name, backup.name)
            return BotState()
        if not isinstance(data, dict):
            return BotState()
        known = {f.name for f in dataclasses.fields(BotState)}
        state = BotState(**{key: value for key, value in data.items() if key in known})
        state.version = STATE_VERSION
        return state

    def save(self, state: BotState) -> None:
        write_json(self.path, asdict(state))


@dataclass
class ControlState:
    enabled: bool = False
    poll_seconds: int = 60
    updated_at: str | None = None
    updated_by: str | None = None
    days_off: list[str] = field(default_factory=list)
    max_jobs_per_day: int = 3
    service_types: list[str] = field(default_factory=lambda: list(SERVICE_TYPES))

    def days_off_slots(self) -> frozenset[Slot]:
        return frozenset(_off_slots(self.days_off))

    def wanted_types(self) -> frozenset[str] | None:
        wanted = frozenset(self.service_types)
        return None if wanted >= frozenset(SERVICE_TYPES) else wanted


def _valid_days(value: object) -> list[str]:
    return off_entries(_off_slots(value)) if isinstance(value, list) else []


def _off_slots(entries: Iterable[object]) -> set[Slot]:
    slots: set[Slot] = set()
    for item in entries:
        text = str(item).strip()
        try:
            slots.add(Slot.parse(text))
        except ValueError:
            try:
                slots |= _covered(dt.date.fromisoformat(text))
            except ValueError:
                continue
    return slots


def _covered(value: dt.date | Slot) -> set[Slot]:
    return {value} if isinstance(value, Slot) else {Slot(value, Half.AM), Slot(value, Half.PM)}


def off_entries(slots: Iterable[Slot]) -> list[str]:
    halves: dict[dt.date, set[Half]] = {}
    for slot in slots:
        halves.setdefault(slot.date, set()).add(slot.half)
    return [
        day.isoformat() if len(found) == 2 else f"{day.isoformat()} {found.pop().value}"
        for day, found in sorted(halves.items())
    ][:MAX_DAYS_OFF]


def _service_types(value: object) -> list[str]:
    if not isinstance(value, list):
        return list(SERVICE_TYPES)
    chosen = {str(item).strip().upper() for item in value}
    return [code for code in SERVICE_TYPES if code in chosen] or list(SERVICE_TYPES)


def _jobs_per_day(value: object) -> int:
    if type(value) is not int:
        return ControlState.max_jobs_per_day
    return min(MAX_JOBS_PER_DAY, max(1, value))


class ControlStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.control_path

    def load(self) -> ControlState:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        try:
            poll = self.settings.clamp_poll(data.get("poll_seconds", self.settings.poll_seconds))
        except (TypeError, ValueError):
            poll = self.settings.poll_seconds
        updated_at, updated_by = data.get("updated_at"), data.get("updated_by")
        return ControlState(
            enabled=data.get("enabled") is True,
            poll_seconds=poll,
            updated_at=updated_at if isinstance(updated_at, str) else None,
            updated_by=updated_by if isinstance(updated_by, str) else None,
            days_off=_valid_days(data.get("days_off")),
            max_jobs_per_day=_jobs_per_day(data.get("max_jobs_per_day")),
            service_types=_service_types(data.get("service_types")),
        )

    def update(
        self,
        *,
        by: str,
        now: dt.datetime,
        enabled: bool | None = None,
        poll_seconds: int | None = None,
        add_day_off: dt.date | Slot | None = None,
        remove_day_off: dt.date | Slot | None = None,
        max_jobs_per_day: int | None = None,
        service_types: list[str] | None = None,
    ) -> ControlState:
        control = self.load()
        if enabled is not None:
            control.enabled = bool(enabled)
        if poll_seconds is not None:
            control.poll_seconds = self.settings.clamp_poll(poll_seconds)
        off = _off_slots(control.days_off)
        if add_day_off is not None:
            off |= _covered(add_day_off)
        if remove_day_off is not None:
            off -= _covered(remove_day_off)
        control.days_off = off_entries(slot for slot in off if slot.date >= now.date())
        if max_jobs_per_day is not None:
            control.max_jobs_per_day = _jobs_per_day(max_jobs_per_day)
        if service_types is not None:
            control.service_types = _service_types(service_types)
        control.updated_at = to_iso(now)
        control.updated_by = by
        write_json(self.path, asdict(control))
        return control


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
