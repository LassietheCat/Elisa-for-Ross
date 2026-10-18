from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Settings
from .models import Slot
from .timeutil import to_iso

log = logging.getLogger(__name__)

STATE_VERSION = 1


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

    def prune(self, today: dt.date, keep_days: int = 7) -> None:
        cutoff = today - dt.timedelta(days=keep_days)
        self.applied = {k: v for k, v in self.applied.items() if Slot.parse(k).date >= cutoff}
        self.attempts = {k: v for k, v in self.attempts.items() if Slot.parse(k).date >= cutoff}
        self.applies_by_day = {
            k: v for k, v in self.applies_by_day.items() if dt.date.fromisoformat(k) >= cutoff
        }


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
        )

    def update(
        self, *, by: str, now: dt.datetime, enabled: bool | None = None, poll_seconds: int | None = None
    ) -> ControlState:
        control = self.load()
        if enabled is not None:
            control.enabled = bool(enabled)
        if poll_seconds is not None:
            control.poll_seconds = self.settings.clamp_poll(poll_seconds)
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
