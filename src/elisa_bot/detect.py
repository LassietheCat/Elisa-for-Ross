from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from typing import AbstractSet, Iterable, Mapping

from .config import Settings
from .models import Opening, Slot, SlotTags
from .state import BotState


@dataclass
class Plan:
    available: set[Slot] = field(default_factory=set)
    booked: set[Slot] = field(default_factory=set)
    new: set[Slot] = field(default_factory=set)
    to_apply: list[Opening] = field(default_factory=list)
    skipped_booked: list[Slot] = field(default_factory=list)
    skipped_day_off: list[Slot] = field(default_factory=list)
    skipped_too_close: list[Slot] = field(default_factory=list)
    skipped_day_full: list[Slot] = field(default_factory=list)
    baseline_only: bool = False


def openings_in(slots: Mapping[Slot, SlotTags], starts: Iterable[dt.datetime] | None) -> list[Opening]:
    open_slots = {slot for slot, tags in slots.items() if tags.available > 0}
    timed = [opening for opening in map(Opening.at, starts or ()) if opening.slot in open_slots]
    untimed = [Opening(slot) for slot in open_slots - {opening.slot for opening in timed}]
    return sorted(timed + untimed, key=_order)


def new_openings(openings: Iterable[Opening], state: BotState, known: Mapping[Slot, AbstractSet[str]]) -> list[Opening]:
    seen = state.seen_slots()
    return [
        opening
        for opening in openings
        if opening.slot not in seen
        or (opening.clock is not None and opening.slot in known and opening.clock not in known[opening.slot])
    ]


def jobs_per_day(slots: Mapping[Slot, SlotTags]) -> Counter[dt.date]:
    jobs: Counter[dt.date] = Counter()
    for slot, tags in slots.items():
        jobs[slot.date] += tags.applied + tags.assigned + tags.confirmed
    return jobs


def plan_month(
    slots: Mapping[Slot, SlotTags],
    state: BotState,
    *,
    today: dt.date,
    settings: Settings,
    openings: list[Opening] | None = None,
    known: Mapping[Slot, AbstractSet[str]] | None = None,
    clicked: AbstractSet[tuple[Slot, str | None]] = frozenset(),
    job_times: Mapping[dt.date, list[dt.datetime] | None] | None = None,
    max_jobs: int = 3,
    already_planned: int = 0,
    baseline: bool | None = None,
    days_off: set[dt.date] | frozenset[dt.date] = frozenset(),
) -> Plan:
    openings = openings_in(slots, None) if openings is None else openings
    candidates = new_openings(openings, state, known or {})
    plan = Plan(
        available={opening.slot for opening in openings},
        booked={slot for slot, tags in slots.items() if tags.booked},
        new={opening.slot for opening in candidates},
    )
    if baseline is None:
        baseline = not state.baseline_done
    if baseline and not settings.apply_on_first_run:
        plan.baseline_only = True
        return plan

    jobs = jobs_per_day(slots)
    times_known = job_times or {}
    applied = state.applied_slots()
    gap = dt.timedelta(minutes=settings.job_gap_minutes)
    cap = settings.max_applies_per_day
    budget = None if cap == 0 else max(0, cap - state.applies_on(today) - already_planned)
    chosen: dict[dt.date, list[Opening]] = {}
    for opening in candidates:
        day = opening.slot.date
        if day < today or (opening.slot, opening.clock) in clicked:
            continue
        if state.attempts.get(str(opening.slot), 0) >= settings.max_attempts_per_slot:
            continue
        if day in days_off:
            plan.skipped_day_off.append(opening.slot)
            continue
        picked = chosen.setdefault(day, [])
        if jobs[day] + len(picked) >= max_jobs:
            plan.skipped_day_full.append(opening.slot)
            continue
        times = times_known.get(day)
        if times is not None and len(times) < jobs[day]:
            times = None
        if opening.start is None or times is None:
            if opening.slot in applied:
                continue
            if opening.slot in plan.booked or any(other.slot == opening.slot for other in picked):
                plan.skipped_booked.append(opening.slot)
                continue
        else:
            others = list(times) + [other.start for other in picked if other.start is not None]
            if any(abs(opening.start - start) < gap for start in others) or any(
                other.start is None and other.slot == opening.slot for other in picked
            ):
                plan.skipped_too_close.append(opening.slot)
                continue
        if budget is not None and len(plan.to_apply) >= budget:
            break
        plan.to_apply.append(opening)
        picked.append(opening)
    return plan


def _order(opening: Opening) -> tuple:
    minutes = -1 if opening.start is None else opening.start.hour * 60 + opening.start.minute
    return opening.slot, minutes
