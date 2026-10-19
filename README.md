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


