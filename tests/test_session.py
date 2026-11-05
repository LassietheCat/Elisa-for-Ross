from __future__ import annotations

import asyncio
import datetime as dt
import logging

import pytest

from elisa_bot.site.errors import InvalidCredentials, SignInPaused, SiteBusy, VerificationRequired
from elisa_bot.site.selectors import Selectors
from elisa_bot.site.session import SignInLimiter, SiteSession
from tests.conftest import wo

pytestmark = pytest.mark.browser


async def test_signs_in_and_opens_the_calendar(mock, session):
    assert await session.ensure_ready() is True
    assert "/my/dashboard/main-dashboard" in session.page.url
    assert await session.calendar_visible()
    assert await session.page.locator("#owo-view").is_visible()
    assert mock.stats()["logins_ok"] == 1


async def test_login_button_waits_for_both_fields(mock, session, settings):
    page = session.page
    await page.goto(settings.base_url + "/login")
    button = page.locator("button[type=submit]")
    assert await button.is_disabled()
    await page.locator("input#email").fill("someone@example.test")
    assert await button.is_disabled()
    await page.locator("input[type=password]").fill("x")
    assert await button.is_enabled()


async def test_rejected_password_stops_without_retrying(mock, session):
    mock.configure(invalid_credentials=True)
    with pytest.raises(InvalidCredentials):
        await session.ensure_ready()
    assert mock.stats()["login_attempts"] == 1


async def test_verification_step_stops_the_run(mock, session):
    mock.configure(require_verification=True)
    with pytest.raises(VerificationRequired):
        await session.ensure_ready()


async def test_elisa_robot_check_stops_after_one_attempt(mock, session):
    mock.configure(require_verification=True, verification_message="We could not verify you are not a robot. Please try again.")
    with pytest.raises(VerificationRequired):
        await session.ensure_ready()
    assert mock.stats()["login_attempts"] == 1


async def test_expired_cookie_session_raises_verification_required(mock, settings, tmp_path):
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(__import__("json").dumps([{
        "name": "mock_session", "value": "stale-token",
        "domain": "127.0.0.1", "path": "/", "secure": False, "httpOnly": True,
    }]))
    env = {
        "ELISA_BASE_URL": settings.base_url,
        "DATA_DIR": str(tmp_path),
        "TIMEZONE": "America/New_York",
        "HEADLESS": "true",
        "TEST_MODE": "true",
        "COOKIES_FILE": str(cookie_path),
    }
    from elisa_bot.config import Settings as S
    stale_settings = S.from_env(env)
    stale = SiteSession(stale_settings, Selectors())
    await stale.start()
    try:
        with pytest.raises(VerificationRequired):
            await stale.ensure_ready()
        assert mock.stats()["login_attempts"] == 0
    finally:
        await stale.close()


async def test_cookie_session_skips_the_login_form(mock, settings, tmp_path):
    cookie_path = tmp_path / "cookies.json"
    first = SiteSession(settings, Selectors())
    await first.start()
    try:
        await first.ensure_ready()
        raw = await first._context.cookies()
        cookie_path.write_text(__import__("json").dumps(raw))
    finally:
        await first.close()

    env = {
        "ELISA_BASE_URL": settings.base_url,
        "DATA_DIR": str(tmp_path),
        "TIMEZONE": "America/New_York",
        "HEADLESS": "true",
        "TEST_MODE": "true",
        "COOKIES_FILE": str(cookie_path),
    }
    from elisa_bot.config import Settings as S
    cookie_settings = S.from_env(env)
    second = SiteSession(cookie_settings, Selectors())
    await second.start()
    try:
        signed_in_now = await second.ensure_ready()
        assert signed_in_now is True
        assert not second._on_login_page()
        assert mock.stats()["logins_ok"] == 1
    finally:
        await second.close()


async def test_signs_out_through_the_user_menu(mock, session):
    assert await session.logout() is False
    await session.ensure_ready()
    assert await session.logout() is True
    assert "/login" in session.page.url
    assert mock.stats()["logouts"] == 1


async def test_signs_in_again_after_the_site_logs_it_out(mock, session):
    await session.ensure_ready()
    mock.expire_sessions()
    await session.page.reload()
    assert await session.ensure_ready() is True
    assert mock.stats()["logins_ok"] == 2


async def test_rate_limited_sign_in_is_busy_not_a_rejected_password(mock, session):
    mock.configure(rate_limited_times=1, retry_after="30")
    with pytest.raises(SiteBusy) as info:
        await session.ensure_ready()
    assert info.value.retry_after == 30
    assert mock.stats()["login_attempts"] == 1


async def test_sign_in_limit_stops_extra_sign_ins(mock, settings):
    session = SiteSession(settings, Selectors(), sign_ins=SignInLimiter(1))
    await session.start()
    try:
        await session.ensure_ready()
        mock.expire_sessions()
        await session.page.reload()
        with pytest.raises(SignInPaused) as info:
            await session.ensure_ready()
        assert 3500 < info.value.retry_after <= 3600
        assert mock.stats()["login_attempts"] == 1
    finally:
        await session.close()


OCT_1, DEC_31, TODAY = dt.date(2026, 10, 1), dt.date(2026, 12, 31), dt.date(2026, 10, 7)


async def test_lists_open_work_orders_for_any_range_by_replaying_the_pages_own_request(mock, session):
    assert await session.open_orders(OCT_1, DEC_31, TODAY) is None
    mock.set_work_orders([
        wo("2026-10-14", "AM", "available", "09:30"),
        wo("2026-11-03", "PM", "available", "13:00"),
        wo("2026-12-08", "AM", "available", "10:00"),
        wo("2027-01-05", "AM", "available"),
        wo("2026-11-04", "AM", "assigned"),
    ])
    await session.ensure_ready()
    await asyncio.sleep(0.5)
    fetches = mock.stats()["calendar_fetches"]
    orders = await session.open_orders(OCT_1, DEC_31, TODAY)
    assert [f"{t:%Y-%m-%d %H:%M}" for t in orders] == ["2026-10-14 09:30", "2026-11-03 13:00", "2026-12-08 10:00"]
    assert len(await session.open_orders(dt.date(2026, 11, 1), dt.date(2026, 11, 30), TODAY)) == 1
    assert mock.stats()["calendar_fetches"] == fetches


async def test_a_refused_list_is_busy(mock, session):
    await session.ensure_ready()
    await asyncio.sleep(0.5)
    mock.configure(list_rate_limited_times=1)
    with pytest.raises(SiteBusy):
        await session.open_orders(OCT_1, DEC_31, TODAY)


async def test_a_signed_out_list_gives_no_answer(mock, session):
    await session.ensure_ready()
    await asyncio.sleep(0.5)
    mock.expire_sessions()
    assert await session.open_orders(OCT_1, DEC_31, TODAY) is None


async def test_reads_the_start_times_of_the_owners_jobs_on_a_day(mock, session):
    assert await session.job_times(dt.date(2026, 11, 4), TODAY) is None
    mock.set_work_orders([
        wo("2026-11-04", "PM", "assigned", "13:30"),
        wo("2026-11-04", "AM", "confirmed", "08:15"),
        wo("2026-11-04", "AM", "available", "10:00"),
        wo("2026-11-05", "AM", "assigned", "09:00"),
    ])
    await session.ensure_ready()
    await asyncio.sleep(0.5)
    times = await session.job_times(dt.date(2026, 11, 4), TODAY)
    assert [f"{t:%Y-%m-%d %H:%M %Z}" for t in times] == ["2026-11-04 08:15 EST", "2026-11-04 13:30 EST"]
    assert await session.job_times(dt.date(2026, 11, 6), TODAY) == []


async def test_an_order_elisa_marks_assigned_is_not_open(mock, session):
    mock.set_work_orders([wo("2026-11-16", "AM", "assigned_listed", "09:00"), wo("2026-11-17", "PM", "available", "13:00")])
    await session.ensure_ready()
    await asyncio.sleep(0.5)
    orders = await session.open_orders(OCT_1, DEC_31, TODAY)
    assert [f"{t:%Y-%m-%d %H:%M}" for t in orders] == ["2026-11-17 13:00"]


async def test_only_wanted_job_types_are_listed(mock, session):
    mock.set_work_orders([
        wo("2026-11-17", "PM", "available", "13:00", sertype="VRI"),
        wo("2026-11-18", "AM", "available", "09:00", sertype="OSI"),
        wo("2026-11-19", "AM", "available", "10:00", sertype="OPI"),
    ])
    await session.ensure_ready()
    await asyncio.sleep(0.5)
    assert len(await session.open_orders(OCT_1, DEC_31, TODAY)) == 3
    session.service_types = frozenset({"VRI"})
    orders = await session.open_orders(OCT_1, DEC_31, TODAY)
    assert [f"{t:%Y-%m-%d %H:%M}" for t in orders] == ["2026-11-17 13:00"]


async def test_the_pages_own_accept_is_learned_then_replayed_for_a_listed_order(mock, session, caplog):
    caplog.set_level(logging.INFO)
    mock.set_work_orders([wo("2026-11-17", "PM", "available", "13:00"), wo("2026-11-18", "AM", "available", "09:00")])
    await session.ensure_ready()
    await asyncio.sleep(0.5)
    first, second = sorted(await session.open_orders(OCT_1, DEC_31, TODAY))
    assert session.listed_count(second) == 1
    assert await session.accept_listed(second) is None
    await session.page.evaluate("onAccept('1', false)")
    await asyncio.sleep(0.2)
    assert "Fast accept ready" in caplog.text
    status, data = await session.accept_listed(second)
    assert status == 200 and data["status"] is True
    assert mock.stats()["accept_calls"] == ["1", "2"]
    assert await session.accept_listed(second, 1) is None


async def test_only_the_pages_plain_accept_is_learned_and_a_refused_replay_is_busy(mock, session, caplog):
    caplog.set_level(logging.INFO)
    mock.set_work_orders([wo("2026-11-17", "PM", "available", "13:00"), wo("2026-11-18", "AM", "available", "09:00")])
    await session.ensure_ready()
    await asyncio.sleep(0.5)
    first, second = sorted(await session.open_orders(OCT_1, DEC_31, TODAY))
    await session.page.evaluate(
        "api('/api/open-work-orders/mock-client/create', {method: 'POST', headers: {'Content-Type': 'application/json'},"
        " body: JSON.stringify({_id: 'mock-client', data: {coi: '9', replocId: 'MOCK1', status: 'WITHDRAW'}})})"
    )
    await asyncio.sleep(0.2)
    assert "Fast accept ready" not in caplog.text and not session.fast_accept_ready
    await session.page.evaluate("onAccept('1', false)")
    await asyncio.sleep(0.2)
    assert session.fast_accept_ready
    mock.configure(apply_mode="refuse", refusal_http_status=429)
    with pytest.raises(SiteBusy):
        await session.accept_listed(second)
