# Elisa bot

Watches the Elisa **Open Work Orders** calendar for the account owner. When a new
opening appears, it accepts **one** work order in that half-day slot, unless the
owner already has a job then. It then sends an email containing only the date
and AM/PM.

It does with the owner's own sign-in what the owner otherwise does by hand: sign
in, open Open Work Orders, click **Available** to reload the calendar, look at this
month and the next two (`MONTHS_TO_CHECK=3`), open an opening and click **Accept**.

The Accept step is based on the Elisa phone app (an Accept button per work order).
The desktop version hasn't been seen on the live site yet, so keep `TEST_MODE=true`
on the real site until it has.

Running it on a server: [RUNBOOK.md](RUNBOOK.md).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/playwright install chromium
cp .env.example .env   # fill it in; keep it private (it is git-ignored)
```

## Commands

```bash
.venv/bin/python -m elisa_bot run              # the always-on bot (see below)
.venv/bin/python -m elisa_bot control          # the password-protected on/off page (see below)
.venv/bin/python -m elisa_bot on | off         # switch the bot on or off from the command line
.venv/bin/python -m elisa_bot status           # the switch and the last check
.venv/bin/python -m elisa_bot check            # sign in, show the months it checks (read-only: clicks nothing)
.venv/bin/python -m elisa_bot site-check       # read-only: does the reader fit the live page? (see below)
.venv/bin/python -m elisa_bot once             # one full run (test mode unless TEST_MODE=false)
.venv/bin/python -m elisa_bot once --live      # one full run that really clicks Accept
.venv/bin/python -m elisa_bot test-email       # send a test alert
.venv/bin/python -m elisa_bot reset-state --yes
```

`check`, `site-check` and `once` each sign in, then sign out at the end, so don't
schedule `once` every 15 seconds; use `run` for that.

To show a full Accept without touching the real site, run the demo against the local
imitation of Elisa (`tests/mock_site/`) in a visible browser:

```bash
.venv/bin/python -m scripts.demo_accept                      # waits for Enter between steps
.venv/bin/python -m scripts.demo_accept --email-to <address>  # also emails the alert, subject "DEMO: …"
```

### run: the always-on bot

`run` keeps going until it is stopped (Ctrl+C or a normal service stop). It starts
**off**. Every second it looks at the switch (`data/control.json`):

- **On:** it signs in once, keeps that session, and checks every `POLL_SECONDS`
  (20 by default, never below `MIN_POLL_SECONDS`, 15). Elisa limits how many requests
  an account can make, so **each check loads one month**, moving one month at a time:
  Oct → Nov → Dec → Nov → Oct … With 3 months that is the middle month every 40 s and
  the outer months every 80 s, one page load per check, never a burst. In a 4-hour
  live test, checking every 15 s drew about 3 refusals an hour; every 20 s drew none.
  Signing in is
  itself a burst of requests, so after a sign-in the bot reads the calendar that just
  loaded and then waits `QUIET_AFTER_SIGN_IN_SECONDS` (90) before the next check.
- **First look per month:** the first time each month is read after the bot starts
  (or after `reset-state`), and the first time a new month comes into view (on the
  1st, the month two ahead), its openings are only recorded, never acted on
  (`APPLY_ON_FIRST_RUN=false`). The bot remembers what it has seen month by month, so
  reading one month never makes another month's openings look new.
- **Off:** it signs out (`LOGOUT_WHEN_OFF=true`), closes the browser and waits.
- **Sign-in rejected** (wrong password or a verification step): it switches itself
  off, emails one alert, and never retries, so the account can't be locked out.
- **A check fails** (the page or the calendar didn't load): it reloads the dashboard. A
  reload is a burst of requests like a sign-in, so it waits at least
  `QUIET_AFTER_SIGN_IN_SECONDS` before the next try, longer if failures repeat (up
  to 5 minutes), then goes back to every `POLL_SECONDS` after a good check. A browser
  that won't start counts as a failed check too; the bot keeps going.
- **The site is busy** (it answers 429 Too Many Requests, at sign-in or during a
  check): the check fails and nothing is accepted. The bot waits longer than for
  other failures (80 s, then about 3, 5 and 11 minutes, up to 15), or as long as the site asks in
  `Retry-After` (up to an hour).
- **A calendar data request fails** (the site's schedule or count request returns an
  error): the check fails and nothing is accepted, because a half-loaded calendar
  could hide a job the owner already has. The page itself still works, so the bot
  doesn't reload it: the next check simply asks for the data again (after 40 s, 80 s,
  … up to 5 minutes if it keeps failing).
- **Sign-ins are capped** at `MAX_SIGN_INS_PER_HOUR` (6). If the site keeps signing
  the bot out, it stops signing in until the hour is up, so it never fights over the
  account or adds to the site's failed-login count.
- **3 failed checks in a row** send one email saying what went wrong. No more emails
  follow until a check works again; then one "working again" email.

### control: the on/off page

`control` serves a small mobile-friendly page on `CONTROL_HOST:CONTROL_PORT`. It
needs `CONTROL_PASSWORD` and `CONTROL_SECRET_KEY` (32+ random characters). The page
shows:

- **ON** or **OFF**, with one big button to switch;
- the check interval (15 s and up);
- the last check and whether it worked, with a warning if the bot has stopped
  checking;
- the slots accepted recently (date and AM/PM only);
- a **Send test alert** button.

Test mode vs live is deliberately **not** on the page; it stays a server setting.

Sign-in uses a signed, HttpOnly, SameSite=Strict cookie that lasts 30 days. Forms carry a
CSRF token, wrong passwords are limited to 5 per 15 minutes, and the page sends strict
security headers. Serve it over HTTPS (`CONTROL_COOKIE_SECURE=true`).

### Email alerts

Alerts go out from the inbox in `SMTP_USERNAME`, with the sender name `SMTP_FROM_NAME`
("Elisa Bot" by default), to the addresses in `ALERT_EMAIL_TO`. Check the setup with
`test-email` or the page's **Send test alert** button.

**AgentMail** (the client's alert inbox, `anya-alerts@agentmail.to`):

```
SMTP_PROVIDER=agentmail
SMTP_USERNAME=anya-alerts@agentmail.to
SMTP_PASSWORD=<AgentMail API key>
```

The SMTP password is an **API key** from console.agentmail.to → API Keys, not the
console sign-in password. The key is shown only once, so keep it in `.env` or a
`SMTP_PASSWORD_FILE` secret. AgentMail sends over port 465 with TLS, and the sender
must be the inbox address. AgentMail shows the inbox's **display name** as the sender
name instead of `SMTP_FROM_NAME`, so set the inbox's display name to "Elisa Bot"
(API: `PATCH /v0/inboxes/<address>` with `display_name`). AgentMail adds a
"Sent via AgentMail" line to each message.

**A normal mailbox:** set `SMTP_PROVIDER` (`gmail`, `outlook`, `aol`, `yahoo`,
`icloud`), the address in `SMTP_USERNAME`, and an **app password** in
`SMTP_PASSWORD`. Gmail, AOL, Yahoo and iCloud require one, created in the account's
security settings. For other providers leave `SMTP_PROVIDER` empty and set
`SMTP_HOST`, `SMTP_PORT`, `SMTP_SSL` and `SMTP_STARTTLS`.

**Which alerts go out** (all on, as the client asked):

| Alert | When | Setting |
|---|---|---|
| Applied | the bot accepted a job | always |
| Please check | Accept was clicked but the result couldn't be confirmed | always |
| Test mode, opening | test mode found an opening and its Accept button | always |
| Missed | an opening was taken by someone else first | `NOTIFY_MISSED` |
| Skipped | a new opening where the owner already has a job | `NOTIFY_SKIPPED` |
| Bot switched off | Elisa rejected the sign-in; the bot stopped itself | always |
| Bot can't check right now | 3 checks in a row failed | always |
| Bot is working again | checks work again after that alert | always |

Exit codes: `0` ok, `1` run failed (or `site-check` found something to fix), `2`
configuration error, `3` the site rejected the sign-in (not retried; fix the
credentials first).

### site-check

A read-only check that the reader fits the live Elisa page. It signs in, opens Open
Work Orders, reloads with **Available**, reads every month it checks, then signs
out. It never opens an opening or clicks Accept. It paces itself to stay under
Elisa's request limit: it waits `QUIET_AFTER_SIGN_IN_SECONDS` after signing in and
`POLL_SECONDS` between months, so it takes about three minutes. A month whose data the
site refused (for example 429 Too Many Requests) is reported as **FAIL**, never as
an empty month. It reports:

- each step: sign-in, the tab, the four status filter buttons, whether a reload's
  loading state was seen, and each month, with every day's AM and PM blocks found;
- a **cross-check**: the calendar page itself asks the site for the owner's jobs per
  day (Applied / Assigned / Confirmed, AM and PM). The check compares that with what
  the reader saw, half-day by half-day, and lists any difference;
- notes: tag words the reader doesn't recognise (in case an opening is labelled
  differently than expected), and the site's own open-work-order count when it
  differs from the calendar.

If something doesn't fit, adjust `site/selectors.py` (or a `SELECTORS_FILE`) and run
it again.

## What one run does

`once` (and each check of `run`) does this; `once` covers every month checked, while
`run` covers one month per check (see above).

1. Signs in (if needed) and opens Open Work Orders.
2. Loads the month: clicks **Available** to reload it if it is already on screen, or
   moves to it with the month arrows. It waits until the load has finished; the old
   calendar stays on screen while the site loads, so it is never read as new.
3. Reads every day's AM and PM tags, works out the new openings, and accepts them
   **before** moving to another month (first come, first served). `once` starts from
   the month on screen, which keeps it to one page load per month.
4. If Elisa refused any of the month's data (429 Too Many Requests or a server error),
   the check fails and nothing is accepted.
   It also compares the calendar with Elisa's own count of open work orders (which the
   page downloads) and logs a warning if they differ, or if a calendar tag can't be
   read. A missed opening shows up in the log at once (counts and dates only).
5. Saves state to `data/state.json` and sends **one** email for the whole run, or
   nothing if nothing happened.

## Safety rules

- **Test mode is the default** (`TEST_MODE=true`): the bot opens the opening, finds
  the Accept button and closes the window without clicking.
- **First run = baseline.** The first run, the first run after `reset-state`, and
  the first look at a month that comes into view only record what is already open
  (`APPLY_ON_FIRST_RUN=false`), because an accepted job can only be withdrawn by
  emailing SOSi.
- **Only new openings.** An opening is handled once, the first time it shows. If
  one appears while in test mode, switching to live later will not accept it.
- **Never double-book:** slots where the owner already has an Applied, Assigned or
  Confirmed job are skipped.
- **One per slot:** if a slot has two openings, only one is accepted.
- **Daily cap** on Accept clicks (`MAX_APPLIES_PER_DAY`, default 10).
- A clicked slot is never clicked again. A slot whose try failed *before* the click
  is retried on the next run (up to `MAX_ATTEMPTS_PER_SLOT`).
- A rejected password is never retried, so the account isn't locked out.
- **Doubtful reads are dropped:** a check where the site rate-limited the bot or a
  calendar data request failed accepts nothing and doesn't update the saved state.
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
deploy/             systemd services, Caddy config, the elisa-bot command for the server
```

If the site's layout changes, adjust `site/selectors.py`, or put overrides in a
JSON file named by `SELECTORS_FILE`.
