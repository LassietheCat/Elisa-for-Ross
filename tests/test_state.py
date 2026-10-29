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
    state = BotState(baseline_done=True, watched_through="2026-12", seen=["2026-10-14 AM"])
    state.record_apply(Slot.parse("2026-10-15 PM"), dt.datetime(2026, 10, 7, 9, 30, tzinfo=ZoneInfo("America/New_York")))
    store.save(state)
    loaded = store.load()
    assert loaded == state
    assert loaded.applied_slots() == {Slot.parse("2026-10-15 PM")}
    assert loaded.applies_on(dt.date(2026, 10, 7)) == 1
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700


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
        jobs={"2026-09": {"slots": {}}, "2026-10": {"slots": {}}, "2026-12": {"slots": {}}},
    )
    state.prune(dt.date(2026, 10, 7))
    assert list(state.applied) == ["2026-10-14 AM"]
    assert list(state.attempts) == ["2026-10-15 PM"]
    assert list(state.applies_by_day) == ["2026-10-07"]
    assert list(state.jobs) == ["2026-10", "2026-12"]


def test_switch_file_defaults_off_and_clamps(tmp_path):
    from elisa_bot.config import Settings
    from elisa_bot.state import ControlState, ControlStore

    now = dt.datetime(2026, 10, 9, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    settings = Settings(data_dir=tmp_path)
    store = ControlStore(settings)
    assert store.load() == ControlState(enabled=False, poll_seconds=60)
    control = store.update(by="web", now=now, enabled=True, poll_seconds=5)
    assert (control.enabled, control.poll_seconds, control.updated_by) == (True, 15, "web")
    assert store.load() == control
    assert stat.S_IMODE(settings.control_path.stat().st_mode) == 0o600
    settings.control_path.write_text("{broken")
    assert store.load().enabled is False


def test_days_off_are_valid_future_dates_only(tmp_path):
    from elisa_bot.config import Settings
    from elisa_bot.state import ControlStore

    now = dt.datetime(2026, 10, 20, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    store = ControlStore(Settings(data_dir=tmp_path))
    store.update(by="test", now=now, add_day_off=dt.date(2026, 10, 22))
    store.update(by="test", now=now, add_day_off=dt.date(2026, 10, 19))
    control = store.update(by="test", now=now, add_day_off=dt.date(2026, 10, 22))
    assert control.days_off == ["2026-10-22"] and control.days_off_slots() == {Slot.parse("2026-10-22 AM"), Slot.parse("2026-10-22 PM")}
    store.path.write_text(json.dumps({"days_off": ["2026-11-03", "not a date", 5]}))
    assert store.load().days_off == ["2026-11-03"]
    assert store.update(by="test", now=now, remove_day_off=dt.date(2026, 11, 3)).days_off == []


def test_half_days_off_merge_into_whole_days(tmp_path):
    from elisa_bot.config import Settings
    from elisa_bot.state import ControlStore

    now = dt.datetime(2026, 10, 28, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    store = ControlStore(Settings(data_dir=tmp_path))
    assert store.update(by="test", now=now, add_day_off=Slot.parse("2026-10-29 AM")).days_off == ["2026-10-29 AM"]
    control = store.update(by="test", now=now, add_day_off=Slot.parse("2026-10-29 PM"))
    assert control.days_off == ["2026-10-29"]
    assert control.days_off_slots() == {Slot.parse("2026-10-29 AM"), Slot.parse("2026-10-29 PM")}
    assert store.update(by="test", now=now, remove_day_off=Slot.parse("2026-10-29 AM")).days_off == ["2026-10-29 PM"]
    assert store.update(by="test", now=now, add_day_off=Slot.parse("2026-10-27 PM")).days_off == ["2026-10-29 PM"]
    store.update(by="test", now=now, add_day_off=dt.date(2026, 10, 30))
    assert store.update(by="test", now=now, add_day_off=Slot.parse("2026-10-30 AM")).days_off == ["2026-10-29 PM", "2026-10-30"]
    assert store.update(by="test", now=now, remove_day_off=dt.date(2026, 10, 29)).days_off == ["2026-10-30"]
    store.path.write_text(json.dumps({"days_off": ["2026-11-03 pm", "2026-11-03 AM", "2026-11-04 XM", "2026-11-05", "2026-11-06 AM"]}))
    assert store.load().days_off == ["2026-11-03", "2026-11-05", "2026-11-06 AM"]


def test_daily_counts_and_activity_stay_small(tmp_path):
    state = BotState()
    state.count(dt.date(2026, 10, 1), "reads")
    state.count(dt.date(2026, 10, 20), "reads")
    state.count(dt.date(2026, 10, 20), "reads")
    state.prune(dt.date(2026, 10, 20))
    assert state.daily == {"2026-10-20": {"reads": 2}}
    for i in range(60):
        state.add_activity(f"2026-10-20T10:{i:02d}:00-04:00", None, "gone", 1.0)
    assert len(state.activity) == 50 and state.activity[-1]["at"] == "2026-10-20T10:59:00-04:00"


def test_job_types_are_known_codes_and_default_to_all(tmp_path):
    from elisa_bot.config import Settings
    from elisa_bot.state import ControlStore

    now = dt.datetime(2026, 10, 27, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    store = ControlStore(Settings(data_dir=tmp_path))
    assert store.load().service_types == ["VRI", "OPI", "OSI"] and store.load().wanted_types() is None
    control = store.update(by="test", now=now, service_types=["vri", "FAX"])
    assert control.service_types == ["VRI"] and control.wanted_types() == frozenset({"VRI"})
    assert store.load().service_types == ["VRI"]
    store.path.write_text(json.dumps({"service_types": []}))
    assert store.load().service_types == ["VRI", "OPI", "OSI"]
