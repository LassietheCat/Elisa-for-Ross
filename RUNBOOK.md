# Elisa bot — Runbook

How to install, run and look after the bot on its server. What the bot does and its
safety rules are in [README.md](README.md); the client's summary is
[HANDOVER.md](HANDOVER.md).

## This deployment

| | |
|---|---|
| Server | Henry's Contabo VPS `207.244.242.218` (Ubuntu 24.04.5, 12 vCPUs, 47 GB), installed Oct 14 by this runbook |
| Bot | On since Oct 14, 19:13, a calendar read every 60 s since Oct 18, quick checks every 30 s since Oct 23 (the open-order list since Oct 26); **live** (`TEST_MODE=false`) since Oct 16, 13:54 (the previous settings are in `/etc/elisa-bot/env.before-live`) |
| Alerts | From "Elisa Bot" <anya-alerts@agentmail.to> to Henry, Anya and Ross (`ALERT_EMAIL_TO`) |
| Logins | `root` by SSH key from Ross's development machine; Henry's root password (to be changed, since it was sent by message) |
| On/off page | `https://anya-elisa.duckdns.org` (DuckDNS name made by Ross; Caddy with a Let's Encrypt certificate and HSTS, in front of 127.0.0.1:8080) |

## Where things live

| What | Where |
|---|---|
| Code and its Python environment | `/opt/elisa-bot` (owned by root; the bot can't change its own code) |
| Settings | `/etc/elisa-bot/env`; secrets in `/etc/elisa-bot/secrets/` (root:elisa, mode 640) |
| State, logs, browser | `/var/lib/elisa-bot` (home of the `elisa` service user) |
| Services | `elisa-bot` (the checker), `elisa-control` (the on/off page on 127.0.0.1:8080), Caddy (HTTPS in front of the page) |

Server: Ubuntu 24.04 with at least 2 GB RAM and 1–2 vCPUs, and a web address pointing
at it for the on/off page (see [HTTPS for the on/off page](#https-for-the-onoff-page)).
Open ports 22, 80 and 443 only. The service file
needs systemd 254 or later (24.04 has 255) so that repeated crashes restart more and
more slowly, up to 15 minutes apart, instead of signing in to Elisa every 30 s.

## Install (once)

```bash
sudo apt update && sudo apt install -y python3-venv caddy ufw
sudo ufw allow OpenSSH && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw --force enable
sudo timedatectl set-timezone America/New_York        # log times in the owner's time zone
sudo useradd --system --create-home --home-dir /var/lib/elisa-bot --shell /usr/sbin/nologin elisa
sudo chmod 750 /var/lib/elisa-bot
```

Copy the code from a development machine (run this there, in the repository; it sends
the project files without `.git`, `.env` or `data/`):

```bash
git ls-files -co --exclude-standard -z | tar --null -czf - -T - | ssh root@<server> 'mkdir -p /opt/elisa-bot && tar -xzf - -C /opt/elisa-bot && chown -R root:root /opt/elisa-bot'
```

Then, on the server:

```bash
cd /opt/elisa-bot
sudo python3 -m venv .venv && sudo .venv/bin/pip install -e .
sudo .venv/bin/playwright install-deps chromium       # system libraries for the browser
sudo -u elisa -H .venv/bin/playwright install chromium
sudo install -m 755 deploy/elisa-bot /usr/local/bin/elisa-bot
sudo install -m 644 deploy/elisa-bot.service deploy/elisa-control.service /etc/systemd/system/
sudo systemctl daemon-reload
```

## Configure

```bash
sudo install -d -m 750 -o root -g elisa /etc/elisa-bot /etc/elisa-bot/secrets
sudo install -m 640 -o root -g elisa .env.example /etc/elisa-bot/env
sudoedit /etc/elisa-bot/env
```

In `/etc/elisa-bot/env`, leave `ELISA_EMAIL`, `ELISA_PASSWORD`, `SMTP_PASSWORD`,
`CONTROL_PASSWORD` and `CONTROL_SECRET_KEY` empty and set:

```
DATA_DIR=/var/lib/elisa-bot/data
TEST_MODE=true
ELISA_EMAIL_FILE=/etc/elisa-bot/secrets/elisa_email
ELISA_PASSWORD_FILE=/etc/elisa-bot/secrets/elisa_password
SMTP_PROVIDER=agentmail
SMTP_USERNAME=anya-alerts@agentmail.to
SMTP_PASSWORD_FILE=/etc/elisa-bot/secrets/smtp_password
ALERT_EMAIL_TO=<recipients, comma-separated>
CONTROL_PASSWORD_FILE=/etc/elisa-bot/secrets/control_password
CONTROL_SECRET_KEY_FILE=/etc/elisa-bot/secrets/control_secret_key
```

Put each secret in its own one-line file: the Elisa email and password, the AgentMail
API key, and a password for the on/off page. The page's signing key is generated:

```bash
for f in elisa_email elisa_password smtp_password control_password; do sudoedit /etc/elisa-bot/secrets/$f; done
python3 -c "import secrets; print(secrets.token_urlsafe(48))" | sudo tee /etc/elisa-bot/secrets/control_secret_key >/dev/null
sudo chown root:elisa /etc/elisa-bot/secrets/* && sudo chmod 640 /etc/elisa-bot/secrets/*
```

The alert sender name is the AgentMail inbox's display name; set it to "Elisa Bot" once
(see README → Email alerts).

## HTTPS for the on/off page

The page needs a web address that points at the server. Either:

- **A free DuckDNS name:** at duckdns.org, sign in, add a sub domain (for example
  `anya-elisa`), set its **current ip** to the server's IP and press **update ip**.
  DuckDNS is on the Public Suffix List, so its certificates don't share a rate limit
  with other users.
- **A domain you own:** at the registrar, add an A record (for example `elisa`)
  pointing at the server's IP.

Put the block from [deploy/Caddyfile](deploy/Caddyfile), with that name, in
`/etc/caddy/Caddyfile`, then `sudo systemctl reload caddy`. Caddy gets and renews the
certificate itself. Moving to another name later is the same two steps.

## Outside monitor

The bot can't email when its whole server is down, so an outside service watches it:

1. At healthchecks.io (free), add a check: period **5 minutes**, grace **5 minutes**. Under
   Integrations, add the email addresses that should be told.
2. Put the check's ping URL in `/etc/elisa-bot/secrets/heartbeat_url` (root:elisa, 640) and set
   `HEARTBEAT_URL_FILE=/etc/elisa-bot/secrets/heartbeat_url` in `/etc/elisa-bot/env`.
3. `sudo systemctl restart elisa-bot`. The check turns green within a minute.

If the server stops, the bot crashes, or it fails for 5 minutes in a row, the pings stop and
the monitor emails within about 10 minutes.

## First start

```bash
elisa-bot test-email          # goes to every address in ALERT_EMAIL_TO
elisa-bot site-check          # read-only, about 3 minutes; must end "the reader fits the live page"
sudo systemctl enable --now elisa-bot elisa-control
```

The bot starts **off**. Wait two minutes after `site-check` (signing in is a burst of
requests), then press **ON** on the page, or run `elisa-bot on` if the page has no
address yet. `journalctl -u elisa-bot -f` should show "Signed in to Elisa", then one
"Checked 2026-10: … (first look, recorded only)" line per month, then a calendar read every
60 s, with quick checks every 30 s in between. They log "Quick checks running" once, then only
when Elisa lists a new open work order or refuses one.

## Day to day

- **One Elisa session at a time.** `check`, `site-check` and `once` sign in on their own,
  and so does a test run on another machine. Before any of them: `elisa-bot off`, wait a
  minute, run it, then `elisa-bot on`. Two sessions double the requests and draw
  "too many requests" refusals.
- **Status:** the on/off page, or `elisa-bot status`.
- **Switch:** the page, or `elisa-bot on` / `elisa-bot off`. The switch survives restarts
  and server reboots.
- **Test alert:** the page's **Send test alert** button, or `elisa-bot test-email`. Both
  go to every recipient.
- **Logs:** `journalctl -u elisa-bot --since today`; also `/var/lib/elisa-bot/data/logs/`.
- **New jobs on the calendar:** `journalctl -u elisa-bot | grep "on the calendar"` lists
  every new, removed or changed job (date and AM/PM), with times, from the calendar
  reads (within about 3 minutes).
- **Live channel:** `journalctl -u elisa-bot | grep "Live channel"` shows the websocket
  connecting after each sign-in, the page joining its work-order rooms, each order
  assigned to someone (and whether the bot had seen it open), and any other event,
  which makes the bot read that month at once.
- **After changing settings or secrets:** `sudo systemctl restart elisa-bot elisa-control`.
- **Stopping:** `sudo systemctl stop elisa-bot` signs out of Elisa first (`KillMode=mixed`
  lets the bot close its own browser).
- **Intervals:** a quick check every `QUICK_CHECK_SECONDS` (20) and a calendar read every
  `POLL_SECONDS` (60; the page's interval changes this one). Don't go faster: on Oct 18, quick
  checks every 10 s drew "too many requests" refusals within minutes. After a refused quick
  check the bot halves the quick-check rate by itself and restores it after 30 calm minutes.

## Alerts and what to do

| Email | Meaning | Do |
|---|---|---|
| Bot switched off (sign-in problem) | Elisa rejected the password or asked for a verification step. The bot stopped itself and won't retry. | Sign in by hand once; update the secret file; restart; switch on. |
| Bot can't check right now | 3 checks in a row failed. | Usually fixes itself ("working again" follows). If not within 30 minutes: read the log, then `elisa-bot off`, `elisa-bot site-check`, `elisa-bot on`. |
| Please check | Accept was clicked but Elisa's answer didn't come or was an error. | Look at Elisa. |
| Refused | Elisa answered the Accept with a refusal; nothing was booked. The log line "Elisa refused the Accept" has a summary of Elisa's message (generic words only) and a fingerprint. | Nothing; send the developer the log line if it repeats. |
| Opening seen but Accept could not be tried | The work-order window or its Accept button wasn't found. Nothing was clicked; the bot retries once. | Look at Elisa; if it repeats, the desktop Accept step differs from what the bot expects (developer). |
| Bot may be missing openings | A calendar tag couldn't be read. | Check Open Work Orders by hand; send the developer the log lines (see below). |
| Applied, test mode, missed, skipped, working again | Normal. | Nothing. |

## When something is wrong

- **The services won't start, "Configuration error" (exit 2):** fix `/etc/elisa-bot/env` or a
  secret file. The service doesn't restart itself after a configuration error.
- **The browser won't start:** run the two `playwright install` lines from Install again
  (after an OS upgrade, or after updating the bot).
- **`site-check` fails a structure step:** Elisa changed its page. Adjust
  `src/elisa_bot/site/selectors.py` (or a `SELECTORS_FILE`), and run `site-check` again
  (with the bot switched off, as above).
- **Many "site_busy" lines in the log:** the interval is too short; raise it on the page.
- **"calendar tag(s) the bot couldn't read":** the bot may be missing openings. Run
  `site-check` (with the bot switched off) and send its output to the developer. Check
  for these with `journalctl -u elisa-bot --since today | grep -E "couldn't read|doesn't know"`.
- **Who changed a setting on the page:** `journalctl -u elisa-control | grep "Page:"`
  (since Oct 27; the page has one shared password, so it shows when, not who).

## Live and test mode

The server runs live (`TEST_MODE=false`) since Oct 16, at the client's request. The
desktop Accept step hadn't been seen on a real opening then, so check the first live
accepts in Elisa. To stop clicking but keep alerts, set `TEST_MODE=true` and run
`sudo systemctl restart elisa-bot`; set it back to `false` to accept again. Openings
seen while in test mode are never accepted later. `MAX_APPLIES_PER_DAY` (10) caps the
accepts per day.

## Updating

Copy the code again with the command from Install, then on the server:

```bash
cd /opt/elisa-bot && sudo .venv/bin/pip install -e .
sudo .venv/bin/playwright install-deps chromium && sudo -u elisa -H .venv/bin/playwright install chromium
sudo systemctl restart elisa-bot elisa-control
```

Run the tests on a development machine, not on the server. `elisa-bot reset-state --yes`
forgets everything the bot has seen; its next look at each month only records.
