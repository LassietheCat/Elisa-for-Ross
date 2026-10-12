from __future__ import annotations

import pytest

from elisa_bot.site.errors import InvalidCredentials, SignInPaused, SiteBusy, VerificationRequired
from elisa_bot.site.selectors import Selectors
from elisa_bot.site.session import SignInLimiter, SiteSession

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
