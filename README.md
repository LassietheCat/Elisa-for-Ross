# Elisa bot

Watches the Elisa **Open Work Orders** calendar for the account owner. When a new
opening appears, it accepts **one** work order in that half-day slot, unless the
owner already has a job then. It then sends an email containing only the date
and AM/PM.

It does with the owner's own sign-in what the owner otherwise does by hand: sign
in, open Open Work Orders, click **Available** to reload the calendar, look at this
month and next month, open an opening and click **Accept**.

## Status

| Day | Feature | State |
|---|---|---|
| Oct 5 | Sign-in; read this month and next month | Done, tested on the local mock |
| Oct 6 | New-opening detection; email alerts | Done, tested on the local mock |
| Oct 7 | Auto-accept; test mode (finds Accept, doesn't click) | Done, tested on the local mock |
| Oct 8 | Fit the reader to the live Elisa page | Next |
| Oct 9 | On/off switch; send from the client's email account | |
| Oct 10 | Server set-up, running around the clock | |

The Accept step is based on the Elisa phone app (an Accept button per work order).
The desktop version hasn't been seen on the live site yet, so keep `TEST_MODE=true`
on the real site until it has.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/playwright install chromium
cp .env.example .env   # fill it in; keep it private (it is git-ignored)
```

## Commands

```bash
.venv/bin/python -m elisa_bot check            # sign in, show both months (read-only: clicks nothing)
.venv/bin/python -m elisa_bot once             # one full run (test mode unless TEST_MODE=false)
.venv/bin/python -m elisa_bot once --live      # one full run that really clicks Accept
.venv/bin/python -m elisa_bot test-email       # send a test alert
.venv/bin/python -m elisa_bot reset-state --yes
```

`once` signs in every time it runs, so don't schedule it every 15 seconds. The
always-on loop arrives with the on/off switch (Oct 9) and keeps one session open.

Exit codes: `0` ok, `1` run failed, `2` configuration error, `3` the site rejected
the sign-in (not retried; fix the credentials first).

## What one run does

1. Signs in (if needed) and opens Open Work Orders.
2. Clicks **Available** to reload the calendar, waiting until the reload has
   finished. The old calendar stays on screen while the site loads, so it is never
   read as new.
3. For this month, then next month: reads every day's AM and PM tags, works out
   the new openings, and accepts them **before** reading the next month (first
   come, first served).
4. Saves state to `data/state.json` and sends **one** email for the whole run, or
   nothing if nothing happened.

## Safety rules

- **Test mode is the default** (`TEST_MODE=true`): the bot opens the opening, finds
  the Accept button and closes the window without clicking.
- **First run = baseline.** The first run, and the first run after `reset-state`,
  only records what is already open (`APPLY_ON_FIRST_RUN=false`), because an
  accepted job can only be withdrawn by emailing SOSi.
- **Only new openings.** An opening is handled once, the first time it shows. If
  one appears while in test mode, switching to live later will not accept it.
- **Never double-book:** slots where the owner already has an Applied, Assigned or
  Confirmed job are skipped.
- **One per slot:** if a slot has two openings, only one is accepted.
- **Daily cap** on Accept clicks (`MAX_APPLIES_PER_DAY`, default 10).
- A clicked slot is never clicked again. A slot whose try failed *before* the click
  is retried on the next run (up to `MAX_ATTEMPTS_PER_SLOT`).
- A rejected password is never retried, so the account isn't locked out.
- **Privacy:** only the date + AM/PM of a slot is stored, logged or emailed. Job
  details in the work-order window are never read. Credentials come from `.env`
  or `*_FILE` secret files, are hidden from `repr()`, and are redacted from logs.
  No screenshots or browser recordings are saved. Logs and state are owner-only (0600).

## Tests

```bash
.venv/bin/pytest -q
```

The browser tests run against `tests/mock_site/`, a local imitation of the Elisa
pages built from the live site's structure (recorded on 2026-10-06). The real site
is never contacted. The mock also covers:

- the hidden duplicate menu link;
- buttons disabled during a reload;
- a blank calendar (exactly one reload, then an error);
- 6-week months;
- a fixed footer over the last row;
- an "Are you sure?" window on top of two Accept buttons (only one is taken);
- openings taken by someone else first;
- site errors;
- rejected sign-ins and verification steps.

## Files

```
src/elisa_bot/
  config.py         settings from env / .env / *_FILE secrets
  models.py         Slot ("2026-10-14 AM"), tag counts, outcomes
  state.py          data/state.json (seen, accepted, last run)
  detect.py         which openings to accept
  notify.py         alert wording + email (SMTP)
  cycle.py          one run, end to end
  logging_setup.py  console + rotating file, secrets redacted
  site/selectors.py everything about the page structure, in one place
  site/session.py   sign-in, Open Work Orders tab
  site/calendar.py  reading the calendar
  site/applier.py   Accept / test mode
tests/              unit tests + browser tests against tests/mock_site/
```

If the site's layout changes, adjust `site/selectors.py`, or put overrides in a
JSON file named by `SELECTORS_FILE`.
