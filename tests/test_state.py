from __future__ import annotations

import datetime as dt
import json
import stat
from zoneinfo import ZoneInfo

from elisa_bot.models import Slot
from elisa_bot.state import BotState, StateStore


def test_missing_file_gives_a_fresh_state(tmp_path):
    state = StateStore(tmp_path / "state.json").load()
    assert state == BotState()


def test_round_trip_and_owner_only_permissions(tmp_path):
    store = StateStore(tmp_path / "data" / "state.json")
    state = BotState(baseline_done=True, seen=["2026-10-14 AM"])
    state.record_apply(Slot.parse("2026-10-15 PM"), dt.datetime(2026, 10, 7, 9, 30, tzinfo=ZoneInfo("America/New_York")))
    store.save(state)
    loaded = store.load()
    assert loaded == state
    assert loaded.applied_slots() == {Slot.parse("2026-10-15 PM")}
    assert loaded.applies_on(dt.date(2026, 10, 7)) == 1
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_corrupt_file_is_moved_aside(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    assert StateStore(path).load() == BotState()
    assert (tmp_path / "state.json.corrupt").exists()


def test_unknown_keys_are_ignored(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"baseline_done": True, "something_old": 1}))
    assert StateStore(path).load().baseline_done is True


def test_prune_forgets_old_slots(tmp_path):
    state = BotState(
        applied={"2026-09-01 AM": "x", "2026-10-14 AM": "y"},
        attempts={"2026-09-02 PM": 1, "2026-10-15 PM": 1},
        applies_by_day={"2026-09-01": 1, "2026-10-07": 2},
    )
    state.prune(dt.date(2026, 10, 7))
    assert list(state.applied) == ["2026-10-14 AM"]
    assert list(state.attempts) == ["2026-10-15 PM"]
    assert list(state.applies_by_day) == ["2026-10-07"]
