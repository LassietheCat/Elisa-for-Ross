from __future__ import annotations

import asyncio
import datetime as dt

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
