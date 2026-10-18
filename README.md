# c17watch

Get a phone notification when a C-17 Globemaster III flies within a set radius
of a location, using public ADS-B data. No UI; runs as a small background process.

## How it works

1. Polls a public ADS-B aggregator for aircraft with ICAO type `C17`
   (`/v2/type/C17`, or ADS-B Exchange's `/v2/mil/` filtered locally).
2. Computes each C-17's great-circle distance to your location **on your machine**.
   Your coordinates are never sent to the data provider.
3. When one enters your radius, sends one push notification (several C-17s in
   formation are grouped into a single message), then stays quiet about that
   aircraft for the cooldown window.

Detection details:

- **Entry-based:** alerts on crossing into the radius, not on every poll while inside.
- **Hysteresis:** the aircraft must go 10% past the radius before it counts as having left,
  so GPS jitter at the edge doesn't cause repeat alerts.
- **Per-aircraft cooldown** (default 30 min), persisted across restarts.
- **Lookahead:** a C-17 at cruise covers ~8–9 miles a minute and can cross a 15-mile
  circle between polls. With `lookahead = true` the app projects its current course and
  speed forward one poll interval and alerts if that path enters the radius
  (tagged "approaching").
- Skips stale positions, aircraft on the ground, and (by default) aircraft enrolled in
  the FAA LADD / PIA privacy programs.

## Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.toml config.toml     # set your lat/lon and radius
cp .env.example .env                   # set NTFY_TOPIC to a long random string
set -a; source .env; set +a
```

**Phone notifications (ntfy, default):** install the ntfy app (iOS/Android), tap
subscribe, and enter the same topic as `NTFY_TOPIC`. On the public ntfy.sh server the
topic name is the only access control, so make it unguessable, e.g.
`python3 -c "import secrets; print('c17-' + secrets.token_urlsafe(16))"`.
Self-host ntfy and set `NTFY_TOKEN` if you want real auth.

**Pushover** is also supported (`method = "pushover"`, set `PUSHOVER_APP_TOKEN` and
`PUSHOVER_USER_KEY`).

## Run

```bash
python -m c17watch --test-notify          # confirm your phone gets a test alert
python -m c17watch --once --dry-run -v    # one poll, print alerts instead of sending
python -m c17watch                        # run continuously
```

Stop with Ctrl-C; state is saved on exit. To keep it running, use a systemd service,
`tmux`, or a container. `--once` works from cron, but then spacing between runs is up
to your cron schedule (budgets are still enforced).

Example systemd unit (`~/.config/systemd/user/c17watch.service`):

```ini
[Service]
WorkingDirectory=%h/c17watch
EnvironmentFile=%h/c17watch/.env
ExecStart=%h/c17watch/.venv/bin/python -m c17watch
Restart=on-failure

[Install]
WantedBy=default.target
```

## Running it all the time

It needs a machine that's always on: a Raspberry Pi, home server or NAS is ideal
(your coordinates never leave home); a small cloud VM also works.

### Docker (recommended)

Works on x86 and ARM (Raspberry Pi 4/5, most NASes).

```bash
cp config.example.toml config.toml   # edit lat/lon/radius
cp .env.example .env                 # set NTFY_TOPIC (no quotes; Compose reads it literally)
docker compose up -d --build
docker compose logs -f               # watch it
docker compose run --rm c17watch --test-notify
```

`restart: unless-stopped` brings it back after crashes and reboots (make sure Docker
itself starts at boot). State lives in the `c17watch-data` volume, so cooldowns and API
budgets survive restarts and upgrades. The container runs as a non-root user with a
read-only filesystem, no capabilities and a 128 MB memory cap.

Update after changing code: `docker compose up -d --build`.

### macOS (launchd), if you just want it on your Mac

Runs whenever the Mac is awake and restarts if it crashes. Save as
`~/Library/LaunchAgents/com.c17watch.plist`, replacing `/Users/you/c17watch`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.c17watch</string>
  <key>WorkingDirectory</key><string>/Users/you/c17watch</string>
  <key>ProgramArguments</key><array>
    <string>/bin/sh</string><string>-c</string>
    <string>set -a; . ./.env; set +a; exec .venv/bin/python -m c17watch</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/you/c17watch/c17watch.log</string>
  <key>StandardErrorPath</key><string>/Users/you/c17watch/c17watch.log</string>
</dict></plist>
```

Then `launchctl load ~/Library/LaunchAgents/com.c17watch.plist`.

### Linux without Docker

See the systemd unit under "Run" above.

## Data providers and rate limiting

| `provider.name` | Key | Published terms (checked Oct 2026) | What the app enforces |
|---|---|---|---|
| `adsblol` (default) | none | Free; ODbL data; limits are dynamic and unpublished | ≥30 s between requests (self-imposed), backoff on 429 |
| `airplaneslive` | none, but **must feed** | Non-commercial; 1 req/s; 500 req/day. Since ~Aug 2026 returns 403 to non-feeders | 500/day budget → polls every ~3 min |
| `adsbexchange` | `ADSBX_RAPIDAPI_KEY` | Paid RapidAPI Community plan, non-commercial; 10,000 req/month on the $10 plan | Monthly budget → ~every 4.7 min; reads RapidAPI's remaining-quota header and slows down if it's lower than expected |

Rate limiting, in layers:

- Minimum spacing per provider; your `poll_interval_s` is raised automatically (with a
  log warning) if it would exceed the provider's limits.
- Daily/monthly budgets spread evenly over the period (5% headroom), persisted in the
  state file so restarts don't reset the count.
- `429` responses honour `Retry-After`; other errors use exponential backoff with jitter,
  capped at 15 minutes.
- 401/403 stops the app rather than hammering a bad key.
- Requests carry an identifying `User-Agent`; set `provider.contact` so API operators can
  reach you instead of just blocking your IP.

Interval trade-off: a 3–5 minute poll can miss a C-17 that clips the edge of a small
radius at cruise speed; lookahead covers most of that, but adsb.lol at 60 s is the most
responsive option. Low-and-slow traffic near an airbase is easy to catch on any provider.

## Troubleshooting

- **`could not connect: ... resolves to 0.0.0.0`** — a DNS blocker (Pi-hole, NextDNS,
  an ad-block DNS profile, or a VPN's filter) is sinkholing the API host. Allowlist
  `api.adsb.lol` (or whichever provider you use).
- **`Connection refused` with a normal-looking IP** — check firewall apps
  (Little Snitch, LuLu), corporate VPN, or a proxy. Test outside Python with
  `curl -v https://api.adsb.lol/v2/type/C17`.
- **One provider server hangs or resets mid-connection** — handled automatically. When a
  host has several IPs (adsb.lol has two), the app tries each in turn within the same poll,
  with TLS still verified against the real hostname, and tries the last working IP first
  next time. You'll see `... failed (ReadTimeout); trying next address` at INFO level.
- **HTTP 403 from airplanes.live** — it now only serves feeders. Switch to `adsblol`.
- **HTTP 429 from adsb.lol** — its limits are load-based; the app backs off
  automatically. Don't set `poll_interval_s` below 30.

## Legal and data notes

- ADS-B is an unencrypted public broadcast. Receiving it, and using data from the
  community aggregators above, is lawful in the US. This app only consumes those public
  APIs; it does not decode anything encrypted, scrape sites, or get around any access
  control.
- Military aircraft often fly with ADS-B off or limited. This app only knows about what is
  broadcast and picked up by community receivers, so silence doesn't mean the sky is empty.
- LADD/PIA-flagged aircraft are skipped by default. Keep that on if you publish.

## Before you publish

- **Licensing:** airplanes.live and the ADS-B Exchange Community API are non-commercial
  only; ADS-B Exchange requires a commercial license for any commercial entity, even for
  internal use. adsb.lol publishes its data under ODbL, which requires attribution (alerts
  include an attribution line). If anything about the project becomes commercial (ads,
  paid tier, hosted service), get a license first.
- **Don't run it as a shared service on one IP.** The design assumes each user runs their
  own copy against their own budget. A hosted version that polls once and fans out to
  many users is a different relationship with the provider; ask them first.
- **Your location:** `config.toml`, `.env` and the state file are git-ignored. Keep it that
  way; use the env-var overrides for anything you share.
- Pick a code license (e.g. MIT or Apache-2.0) and add a `LICENSE` file.
- `pip install .` works via `pyproject.toml` and installs a `c17watch` command.

## Tests

```bash
python -m unittest discover -s tests -t .
```

Covers distance and lookahead math, entry/hysteresis/cooldown logic, filters, budget
and backoff behaviour, RapidAPI quota headers, config validation, state persistence
across restarts, and failed-notification retry.

## Layout

```
c17watch/
  __main__.py    CLI
  config.py      TOML + env loading, validation
  providers.py   API definitions, response parsing
  ratelimit.py   spacing, budgets, backoff
  watcher.py     detection logic and poll loop
  geo.py         distance, bearing, closest-approach math
  notify.py      ntfy / Pushover / console
  state.py       atomic JSON state file
```
