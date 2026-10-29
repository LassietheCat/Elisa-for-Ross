# Elisa bot

Watches the Elisa **Open Work Orders** calendar for the account owner. When a new
opening of a job type chosen on the page (VRI, OPI or OSI) appears, it accepts it if
the owner has fewer jobs that day than the maximum set on the page (3 by default) and
it starts at least 3 hours from each of her other jobs that day. It then sends an email containing only the date and AM/PM.

It does with the owner's own sign-in what the owner otherwise does by hand: sign
in, open Open Work Orders, click **Available** to reload the calendar, look at this
month and the next two (`MONTHS_TO_CHECK=3`), open an opening and click **Accept**.

The Accept step is based on the Elisa phone app (an Accept button per work order).
The deployed bot has run live (`TEST_MODE=false`) since Oct 16. The desktop Accept
step hadn't been seen on a real opening at release, so check the first live accepts
in Elisa. If the page differs, the bot emails "please check" or "could not be tried"
rather than failing silently.

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

- **On:** it signs in once and keeps that session.
  - **Quick check, every `QUICK_CHECK_SECONDS` (30):** it asks Elisa for the open work
    orders in all the months it checks, in one request: the open-order list request the
    calendar page itself makes, replayed with the bot's date range. When an order is
    listed that wasn't in the previous answer, the bot reads that order's month at once
    and accepts it under the usual rules. Openings are often taken within 1–3 minutes,
    so this is what makes the bot fast. The answer holds job details; the bot keeps only
    each order's start time, in memory. (Elisa's count request, used until Oct 26, always
    answers 0 open orders for an interpreter account, so it found nothing.)
  - **Calendar read, every `POLL_SECONDS` (60 by default, never below
    `MIN_POLL_SECONDS`, 15):** it reads one month, sweeping one month at a time
    (Oct → Nov → Dec → Dec → Nov → Oct → Oct …). This keeps the page and the sign-in
    fresh, gives the quick check its request to replay, and catches anything the quick
    check misses. The page keeps each month's open work orders once it has loaded them, and
    moving between months reuses that list; only the **Available** button loads it
    again. The bot watches the page's own list requests and clicks Available whenever
    the month it is about to read wasn't loaded during this check, so every read is
    fresh. The sweep only ever moves to a month the page hasn't stored since its last
    reload, or reloads the month it is on, so each read costs one list load and one
    count request.
  - **Open orders on the calendar** are number badges (`calender-cell-badge`, for
    example "1") with the court code beside them, grouped by court in each half-day.
    A badge also stays for an order that is already taken (it turns orange or green),
    so when Elisa's open-order list loaded with the read, the list decides: an order
    marked Assigned or Confirmed, or with an assign or confirm record, is taken, and a
    badge only marks where to click. Every visible badge is remembered as seen, so a
    taken order's badge never looks new later. If one open order in the list has no
    readable start time, badges count as openings only on that order's date. If the
    list can't be read at all, the bot accepts nothing from that read and emails "the
    bot may be missing openings" once; a list request that fails fails the check.
  - **Live channel:** the page keeps a websocket open to Elisa and joins a room for
    each of the owner's languages. The bot listens to what arrives on its own page; it
    adds no requests. Each event is logged by name, date and AM/PM only. An order
    being assigned to someone (`coi_assigned`) is only logged, saying whether the bot
    had seen it open. Any other event makes the bot read that month at once (at most
    one such read every 10 s, and no more than 30 per event name an hour).
  - Elisa limits how many requests an account can make: count requests every 10 s drew
    refusals within minutes, and every 20 s about twice an hour.
    When a quick check is refused, quick checks pause until the next calendar read and
    then run half as often (down to every 2 minutes); after 30 minutes without a
    refusal they step back up (halving the interval, never below `QUICK_CHECK_SECONDS`), one step per calm half hour. They keep their own schedule across
    calendar reads, so a slow quick check still runs when calendar reads come sooner.
    They also pause while Elisa refuses calendar reads and during the quiet periods
    below.

  Signing in is itself a burst of requests, so after a sign-in the bot reads the
  calendar that just loaded and then waits `QUIET_AFTER_SIGN_IN_SECONDS` (90) before
  the next check.
- **First look per month:** the first time each month is read after the bot starts
  (or after `reset-state`), and the first time a new month comes into view (on the
  1st, the month two ahead), its openings are only recorded, never acted on
  (`APPLY_ON_FIRST_RUN=false`). The bot remembers what it has seen month by month, so
  reading one month never makes another month's openings look new.
- **Activity record:** each opening it handles is logged with the seconds from the
  quick check seeing it to the result, and kept in `data/state.json` (last 50: time,
  date and AM/PM, outcome). If a quick check sees a new opening but it's gone by the
  time the calendar loads, that's recorded as "gone": taken before the bot could read it.
- **Job watch:** the bot logs every change to the owner's jobs, by date and AM/PM only,
  to show when and how new jobs arrive. Each calendar read compares the month with its previous read and logs lines such as
  "New job on the calendar: Thu Oct 29 PM, now 1 Assigned (was none at 14:28:41)", or
  "Job gone" / "Job changed". The previous read of each month is kept in
  `data/state.json`. This adds no requests.
- **Heartbeat:** with `HEARTBEAT_URL` set (for example a healthchecks.io check), the bot
  pings it every `HEARTBEAT_SECONDS` (60) while it's working, or deliberately switched
  off. If the server stops, the bot crashes, or nothing has gone well for 5 minutes, the
  pings stop and the outside monitor emails you. The bot can't send its own alert when
  its whole server is down.
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
  other failures (4 and 8 minutes, then 15), or as long as the site asks in
  `Retry-After` (up to an hour).
- **A calendar data request fails** (the site's schedule or count request returns an
  error): the check fails and nothing is accepted, because a half-loaded calendar
  could hide a job the owner already has. The page itself still works, so the bot
  doesn't reload it: the next check simply asks for the data again (after 2 minutes,
  then 4, up to 5 if it keeps failing). Quick checks carry on meanwhile.
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
- how often it checks: the quick check, and the calendar interval (15 s and up), which the page can change;
- the last check and whether it worked, with a warning if the bot has stopped
  checking;
- **Right now:** the quick checks (last answer, and Elisa's count of open orders, or
  "paused"), since when the bot is signed in to Elisa, the outside alarm, and today's
  numbers (calendar reads, failures, accepted, missed). The bot writes this to
  `data/status.json` about every 15 s; the page warns when it stops being updated;
- **Recent activity:** the last 10 openings the bot handled (date and AM/PM, outcome,
  seconds after it noticed them), including openings that were gone before it could
  read them;
- the slots accepted recently (date and AM/PM only);
- **Days off:** dates or half-days when the bot doesn't accept jobs (a date picker plus
  Whole day / AM only / PM only; AM and PM of one date merge into a whole day; past
  days drop off by themselves). AM means a start before noon Eastern, PM noon or later,
  as in the emails. Openings then are only reported, as "day off";
- **Jobs per day:** the most jobs a day the bot will fill up to (1–4, default 3);
- **Job types:** the service types the bot accepts: VRI (video), OPI (phone), OSI (on
  site); at least one, all three by default. Orders of other types are ignored;
- a **Send test alert** button.

The page refreshes itself every minute, and can be saved to a phone's home screen: it
has an app icon and a web app manifest, so it opens like an app.

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
| Refused | Elisa answered the Accept with a refusal (other than "No Longer Available") | always |
| Test mode, opening | test mode found an opening and its Accept button | always |
| Missed | an opening was taken by someone else first | `NOTIFY_MISSED` |
| Skipped | a new opening where the owner already has a job | `NOTIFY_SKIPPED` |
| Bot switched off | Elisa rejected the sign-in; the bot stopped itself | always |
| Bot can't check right now | 3 checks in a row failed | always |
| Bot is working again | checks work again after that alert | always |
| Bot may be missing openings | a calendar tag can't be read | always |

Exit codes: `0` ok, `1` run failed (or `site-check` found something to fix), `2`
configuration error, `3` the site rejected the sign-in (not retried; fix the
credentials first).

### site-check

A read-only check that the reader fits the live Elisa page. It signs in, opens Open
Work Orders, reloads with **Available**, reads every month it checks, then signs
out. It never opens an opening or clicks Accept. It paces itself to stay under
Elisa's request limit: it waits `QUIET_AFTER_SIGN_IN_SECONDS` after signing in and
`POLL_SECONDS` between months, so it takes about four minutes. A month whose data the
site refused (for example 429 Too Many Requests) is reported as **FAIL**, never as
an empty month. It reports:

- each step: sign-in, the tab, the four status filter buttons, whether a reload's
  loading state was seen, and each month, with every day's AM and PM blocks found;
- a **cross-check**: the calendar page itself asks the site for the owner's jobs per
  day (Applied / Assigned / Confirmed, AM and PM). The check compares that with what
  the reader saw, half-day by half-day, and lists any difference;
- notes: tag words the reader doesn't recognise (in case an opening is labelled
  differently than expected).

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
   It logs a warning if a calendar tag can't be read (dates only).
5. Saves state to `data/state.json` and sends **one** email for the whole run, or
   nothing if nothing happened.

## Safety rules

- **Test mode is the code's default** (`TEST_MODE=true`): the bot opens the opening,
  finds the Accept button and doesn't click it. The server runs with `TEST_MODE=false`.
- **How Accept works on Elisa:** clicking a badge opens that court's list of work
  orders under the calendar. The bot clicks an Accept button whose row shows the
  opening's start time (found by its `onAccept` handler, since Elisa fills in the label
  itself). For an on-site job Elisa first shows the driving distance; the bot answers
  **Continue**. It then reads Elisa's own answer to the request (`status` true or
  false): true is "Applied"; false with "No Longer Available" is "Missed"; any other
  false is "Elisa refused it"; no answer or an HTTP error is "please check". A taken
  order can sit next to the open one (Elisa still shows Accept for an Assigned order),
  so after a refusal or "No Longer Available" the bot tries the next matching row,
  then the next badge in the half-day, up to 3 tries, and stops at the first success.
  Elisa assigns the job the moment it is accepted, and an assigned job can only be
  withdrawn by emailing SOSi, so every Accept is binding. If Elisa shows a pop-up
  window instead of the list, the bot uses the Accept button in that window.
- **Missed openings are reported:** if a calendar tag or Elisa's open-order list can't
  be read, one "the bot may be missing openings" email goes out.
- **Minimum notice** (`MIN_NOTICE_MINUTES`, 0 = off): openings that start sooner than
  this are not accepted and are emailed as "too soon (minimum notice N hours)". Without
  a start time, the half-day's earliest possible start counts (midnight for AM, noon
  for PM).
- **Page changes are logged** by the page service: `journalctl -u elisa-control | grep "Page:"`
  shows days off added or removed, the switch, the interval, jobs per day and job types.
- **Only the chosen job types:** an open order counts only if its service type
  (`sertype` in Elisa's list) is ticked under **Job types** on the page, in both the
  quick check and the calendar reads; an order without a type doesn't count. Before
  clicking, the bot checks the type on the order's row again and only clicks Accept on
  a row of a chosen type.
- **First run = baseline.** The first run, the first run after `reset-state`, and
  the first look at a month that comes into view only record what is already open
  (`APPLY_ON_FIRST_RUN=false`), because an accepted job can only be withdrawn by
  emailing SOSi.
- **Only new openings.** An opening is handled once, the first time it shows. If
  one appears while in test mode, switching to live later will not accept it.
- **Several jobs a day, never overlapping:** an opening is accepted only if the owner
  has fewer jobs that day (Applied, Assigned or Confirmed, plus the bot's accepts in
  this check) than **Jobs per day** on the page (1–4, default 3), and its start time
  is at least `JOB_GAP_MINUTES` (180) from the start of every other job she has that
  day, in Eastern time. Opening times come from the open-order list the page loads;
  her job times from Elisa's job-details request (the one the page makes when a job
  is clicked), made only when a new opening falls on a day she already has jobs.
  Times are used in memory only: the state file, the log and emails hold date and
  AM/PM. If her job times can't be loaded, the old rule applies: only a half-day
  with no job, one per half-day. Skips are emailed as "less than 3 hours from another
  job" or "already 3 jobs that day".
- **Two openings, same start time:** only one is accepted (the second is too close).
- **Daily cap** on Accept clicks (`MAX_APPLIES_PER_DAY`, default 10).
- A clicked slot is never clicked again. A slot whose try failed *before* the click
  is retried on the next run (up to `MAX_ATTEMPTS_PER_SLOT`).
- A rejected password is never retried, so the account isn't locked out.
- **Doubtful reads are dropped:** a check where the site rate-limited the bot or a
  calendar data request failed accepts nothing and doesn't update the saved state.
- **Privacy:** only the date + AM/PM of a slot is stored, logged or emailed. Job
  details in the work-order list and in Elisa's live events are never stored, logged
  or sent. When Elisa refuses an Accept, the log keeps only the generic words of its
  message (other words become "…", numbers are dropped), its length and a short
  fingerprint, so repeats can be grouped. Credentials come from `.env`
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
