from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pytest

from elisa_bot.config import ConfigError, Settings, read_env_file
from elisa_bot.logging_setup import RedactingFilter
from elisa_bot.models import Half, Slot
from elisa_bot.site.selectors import Selectors, load_selectors


def test_defaults_are_safe():
    settings = Settings.from_env({})
    assert settings.test_mode is True
    assert settings.apply_on_first_run is False
    assert settings.base_url == "https://www.sosi1.com"
    assert settings.max_sign_ins_per_hour == 6


def test_secrets_can_come_from_files_and_never_show_in_repr(tmp_path):
    secret = tmp_path / "pw"
    secret.write_text("file-password\n")
    settings = Settings.from_env({"ELISA_EMAIL": "owner@example.test", "ELISA_PASSWORD_FILE": str(secret)})
    assert settings.elisa_password == "file-password"
    assert "file-password" not in repr(settings)
    assert "owner@example.test" not in repr(settings)


def test_bad_values_name_the_setting_not_the_value():
    with pytest.raises(ConfigError, match="TEST_MODE") as info:
        Settings.from_env({"TEST_MODE": "perhaps"})
    assert "perhaps" not in str(info.value)


def test_missing_credentials_are_reported():
    with pytest.raises(ConfigError, match="ELISA_EMAIL, ELISA_PASSWORD"):
        Settings.from_env({}).require_site_credentials()


def test_env_file_is_read_and_real_env_wins(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nTEST_MODE=false\nALERT_EMAIL_TO='a@example.test, b@example.test'\n")
    assert read_env_file(env_file)["TEST_MODE"] == "false"
    settings = Settings.from_env({"TEST_MODE": "true"}, env_file=env_file)
    assert settings.test_mode is True
    assert settings.alert_email_to == ("a@example.test", "b@example.test")


def test_redacting_filter_hides_secrets_in_messages_and_tracebacks():
    filt = RedactingFilter(["hunter2", "owner@example.test"])
    try:
        raise RuntimeError("bad password hunter2")
    except RuntimeError:
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "login %s failed", ("owner@example.test",), exc_info=__import__("sys").exc_info())
    filt.filter(record)
    assert record.getMessage() == "login [REDACTED] failed"
    assert "hunter2" not in record.exc_text


def test_selector_overrides(tmp_path):
    path = tmp_path / "selectors.json"
    path.write_text('{"month_title": ".month"}')
    assert load_selectors(path).month_title == ".month"
    path.write_text('{"no_such_key": 1}')
    with pytest.raises(ConfigError, match="no_such_key"):
        load_selectors(path)
    assert load_selectors(None) == Selectors()


def test_slot_format_label_and_order():
    slot = Slot.parse("2026-10-14 pm")
    assert slot == Slot(dt.date(2026, 10, 14), Half.PM)
    assert str(slot) == "2026-10-14 PM"
    assert slot.label() == "Wed Oct 14 PM"
    assert sorted([slot, Slot.parse("2026-10-14 AM")])[0].half is Half.AM


def test_smtp_provider_presets():
    aol = Settings.from_env({"SMTP_PROVIDER": "aol", "SMTP_USERNAME": "owner@example.test"})
    assert (aol.smtp_host, aol.smtp_port, aol.smtp_ssl, aol.smtp_starttls) == ("smtp.aol.com", 465, True, False)
    assert aol.smtp_from == "owner@example.test"
    gmail = Settings.from_env({"SMTP_PROVIDER": "Gmail"})
    assert (gmail.smtp_host, gmail.smtp_port, gmail.smtp_ssl, gmail.smtp_starttls) == ("smtp.gmail.com", 587, False, True)
    custom = Settings.from_env({"SMTP_PROVIDER": "gmail", "SMTP_PORT": "465", "SMTP_SSL": "true", "SMTP_FROM": "alerts@example.test"})
    assert (custom.smtp_port, custom.smtp_ssl, custom.smtp_starttls, custom.smtp_from) == (465, True, False, "alerts@example.test")
    with pytest.raises(ConfigError, match="SMTP_PROVIDER"):
        Settings.from_env({"SMTP_PROVIDER": "carrier-pigeon"})


def test_check_interval_limits():
    assert Settings.from_env({}).poll_seconds == 15
    with pytest.raises(ConfigError, match="POLL_SECONDS must be at least 15"):
        Settings.from_env({"POLL_SECONDS": "5"})
    with pytest.raises(ConfigError, match="MIN_POLL_SECONDS"):
        Settings.from_env({"MIN_POLL_SECONDS": "1"})
    settings = Settings.from_env({"MIN_POLL_SECONDS": "10", "POLL_SECONDS": "10"})
    assert (settings.clamp_poll(3), settings.clamp_poll(9999)) == (10, 600)


def test_control_secrets_are_checked_and_hidden():
    with pytest.raises(ConfigError, match="CONTROL_PASSWORD, CONTROL_SECRET_KEY"):
        Settings.from_env({}).require_control_secrets()
    with pytest.raises(ConfigError, match="at least 32"):
        Settings.from_env({"CONTROL_PASSWORD": "p", "CONTROL_SECRET_KEY": "short"}).require_control_secrets()
    settings = Settings.from_env({"CONTROL_PASSWORD": "pw-123", "CONTROL_SECRET_KEY": "k" * 32})
    settings.require_control_secrets()
    assert "pw-123" not in repr(settings) and "pw-123" in settings.secret_values()


def test_client_choices_are_the_defaults():
    settings = Settings.from_env({})
    assert settings.months_to_check == 3
    assert settings.notify_missed is True and settings.notify_skipped is True
    assert Settings.from_env({"MONTHS_TO_CHECK": "2"}).months_to_check == 2
    with pytest.raises(ConfigError, match="MONTHS_TO_CHECK"):
        Settings.from_env({"MONTHS_TO_CHECK": "0"})


def test_agentmail_preset():
    settings = Settings.from_env({"SMTP_PROVIDER": "agentmail", "SMTP_USERNAME": "anya-alerts@agentmail.to"})
    assert (settings.smtp_host, settings.smtp_port, settings.smtp_ssl, settings.smtp_starttls) == ("smtp.agentmail.to", 465, True, False)
    assert settings.smtp_from == "anya-alerts@agentmail.to"
