from __future__ import annotations

import datetime as dt
import json
import logging
from zoneinfo import ZoneInfo

from elisa_bot.models import Half, Slot
from elisa_bot.site.live import LiveChannel, event_slot, parse_event

NEW_YORK = ZoneInfo("America/New_York")


def order(day: str, sched: str = "14:00", zone: str = "America/New_York") -> dict:
    return {"deldate": f"{day}T00:00:00.000Z", "sched": sched, "courtTimezone": zone}


def frame(name: str, data: dict) -> str:
    return "42" + json.dumps([name, data])


def test_socket_io_event_frames_are_read_and_everything_else_is_ignored():
    assert parse_event('42["coi_assigned",{"coi":1}]') == ("coi_assigned", [{"coi": 1}])
    assert parse_event('42/admin,7["subscribe","OPEN_WO_X"]') == ("subscribe", ["OPEN_WO_X"])
    for text in ("2", "3", '0{"sid":"x"}', "40", "42not json", '42{"a":1}', "42[]", "42[5]", b'42["x"]', None):
        assert parse_event(text) is None


def test_an_event_is_placed_on_the_viewers_clock_like_the_calendar():
    assert event_slot([order("2026-10-21", "08:30", "America/Los_Angeles")], NEW_YORK) == (dt.date(2026, 10, 21), Half.AM)
    assert event_slot([order("2026-10-21", "10:00:00", "America/Los_Angeles")], NEW_YORK) == (dt.date(2026, 10, 21), Half.PM)
    assert event_slot([{"deldate": "2026-10-21"}], NEW_YORK) == (dt.date(2026, 10, 21), None)
    assert event_slot([order("2026-10-21", "9:15", "Not/AZone")], NEW_YORK) == (dt.date(2026, 10, 21), None)
    assert event_slot([], NEW_YORK) == (None, None)
    assert event_slot(["text"], NEW_YORK) == (None, None)


def test_new_events_trigger_one_read_at_a_time_and_assigned_orders_are_only_logged(caplog):
    caplog.set_level(logging.INFO)
    now = [0.0]
    live = LiveChannel(NEW_YORK, timer=lambda: now[0])
    live.sent('42["subscribe","OPEN_WO_ABC"]')
    live.sent('42["subscribe","PG_CONNECTION"]')
    live.received('42["pg_status_update",{}]')
    live.received('42["pg_status_update",{}]')
    live.saw_open([Slot.parse("2026-10-29 PM")])
    now[0] = 30
    live.received(frame("coi_assigned", order("2026-10-29")))
    live.received(frame("coi_assigned", order("2026-11-03", "09:00")))
    assert live.take_trigger() == (False, None)
    live.received(frame("coi_created", order("2026-11-03", "09:00")))
    assert live.take_trigger() == (True, [(2026, 11)])
    now[0] = 35
    live.received('42["coi_created",{"deldate":"2026-12-01T00:00:00.000Z"}]')
    assert live.take_trigger() == (False, None)
    now[0] = 40
    live.received('42["something_else",{}]')
    assert live.take_trigger() == (True, None)
    messages = [r.getMessage() for r in caplog.records]
    assert live.order_rooms == 1 and "Live channel: the page joined a work-order room (1 so far)" in messages
    assert messages.count("Live channel: first 'pg_status_update' event") == 1
    assert "Live channel: a work order for Thu Oct 29 PM was assigned; the bot saw it open 30 s before" in messages
    assert "Live channel: a work order for Tue Nov 3 AM was assigned; the bot had not seen it open" in messages
    assert "Live channel: 'coi_created' event for Tue Nov 3 AM; reading that month now" in messages
    assert live.counts == {"pg_status_update": 2, "coi_assigned": 2, "coi_created": 2, "something_else": 1}


def test_a_flood_of_one_event_stops_triggering_reads_until_it_calms_down(caplog):
    now = [0.0]
    live = LiveChannel(NEW_YORK, timer=lambda: now[0])
    fired = 0
    for i in range(35):
        now[0] = i * 11.0
        live.received('42["noisy",{}]')
        fired += live.take_trigger()[0]
    assert fired == 30
    assert caplog.text.count("over 30 'noisy' events in an hour") == 1
    now[0] = 3700.0
    live.received('42["noisy",{}]')
    assert live.take_trigger()[0] is True
