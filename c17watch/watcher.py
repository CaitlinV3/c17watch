"""Detection logic and the polling loop."""

from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass

import requests

from .config import Config
from .geo import bearing_deg, closest_approach_miles, compass_point, haversine_miles
from .health import Health, fmt_duration
from .notify import Message, Notifier
from .providers import Aircraft, AuthError, Provider, ProviderError, RateLimited, user_agent
from .ratelimit import BudgetExhausted, RateLimiter
from .state import StateStore

log = logging.getLogger(__name__)

EXIT_HYSTERESIS = 1.1  # must go 10% past the radius before counting as "left"
FORGET_AFTER_S = 10 * 60  # unseen this long -> next sighting counts as a new entry


@dataclass
class Sighting:
    aircraft: Aircraft
    distance_mi: float
    bearing_deg: float
    predicted: bool  # True = projected to enter before the next poll


def evaluate(
    cfg: Config,
    aircraft: list[Aircraft],
    tracked: dict[str, dict],
    now: float,
    horizon_s: float,
) -> list[Sighting]:
    """Update per-aircraft state and return the sightings that should alert.

    Pure function apart from mutating `tracked`, which makes it easy to test.
    """
    alerts: list[Sighting] = []

    for ac in aircraft:
        if (ac.type_code or "").upper() != cfg.aircraft_type:
            continue
        if ac.lat is None or ac.lon is None or not ac.hex:
            continue
        if ac.seen_pos is not None and ac.seen_pos > cfg.max_position_age_s:
            continue
        if cfg.ignore_on_ground and ac.on_ground:
            continue
        if cfg.respect_privacy_flags and ac.privacy_flagged:
            continue

        st = tracked.setdefault(ac.hex, {"inside": False, "last_alert": None, "last_seen": None})
        last_seen = st.get("last_seen")
        if last_seen is not None and now - last_seen > FORGET_AFTER_S:
            st["inside"] = False
        st["last_seen"] = now

        dist = haversine_miles(cfg.lat, cfg.lon, ac.lat, ac.lon)
        was_inside = st["inside"]

        if was_inside:
            st["inside"] = dist <= cfg.radius_miles * EXIT_HYSTERESIS
            continue

        inside_now = dist <= cfg.radius_miles
        predicted = False
        if not inside_now and cfg.lookahead:
            # Catch fast passes that would enter and leave between two polls.
            predicted = (
                closest_approach_miles(cfg.lat, cfg.lon, ac.lat, ac.lon, ac.track, ac.gs, horizon_s)
                <= cfg.radius_miles
            )

        if not (inside_now or predicted):
            continue

        # Only flip to "inside" for real entries; a prediction alone might not
        # pan out, and we still want the real entry tracked for hysteresis.
        if inside_now:
            st["inside"] = True

        last_alert = st.get("last_alert")
        if last_alert is not None and now - last_alert < cfg.cooldown_s:
            continue
        st["last_alert"] = now
        alerts.append(Sighting(ac, dist, bearing_deg(cfg.lat, cfg.lon, ac.lat, ac.lon), predicted))

    return alerts


def format_message(sightings: list[Sighting], provider: Provider) -> Message:
    def line(s: Sighting) -> str:
        ac = s.aircraft
        name = ac.callsign or ac.registration or ac.hex.upper()
        alt = "ground" if ac.on_ground else (f"{ac.alt_baro:,} ft" if isinstance(ac.alt_baro, int) else "alt ?")
        spd = f"{ac.gs:.0f} kt" if ac.gs is not None else "spd ?"
        where = f"{s.distance_mi:.1f} mi {compass_point(s.bearing_deg)}"
        tag = " (approaching)" if s.predicted else ""
        reg = f" [{ac.registration}]" if ac.registration and ac.registration != name else ""
        return f"{name}{reg}: {where}, {alt}, {spd}{tag}"

    n = len(sightings)
    title = "C-17 overhead" if n == 1 else f"{n} C-17s overhead"
    body = "\n".join(line(s) for s in sightings) + f"\n{provider.attribution}"
    click = provider.map_url.format(hex=sightings[0].aircraft.hex)
    return Message(title=title, body=body, click_url=click)


class Watcher:
    def __init__(self, cfg: Config, provider: Provider, notifier: Notifier, session: requests.Session):
        self.cfg = cfg
        self.provider = provider
        self.notifier = notifier
        self.session = session
        self.session.headers["User-Agent"] = user_agent(cfg.contact)
        self.state = StateStore(cfg.state_path)
        self.limiter = RateLimiter(provider, cfg.poll_interval_s, self.state.usage, cfg.max_backoff_s)
        self.health = Health(self.state.data.setdefault("health", {}), time.time())
        self._stop = threading.Event()

    def stop(self, *_args) -> None:
        log.info("stopping")
        self._stop.set()

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

    def poll_once(self) -> int:
        """One request + evaluation. Returns number of alerts sent.

        Raises BudgetExhausted / AuthError / ProviderError to the caller.
        """
        wait = self.limiter.seconds_until_allowed()
        if wait > 0:
            self._stop.wait(wait)
            if self._stop.is_set():
                return 0

        self.limiter.record_request()
        try:
            aircraft, headers = self.provider.fetch(self.session, self.cfg.aircraft_type)
        except RateLimited as e:
            delay = self.limiter.record_failure(e.retry_after)
            log.warning("%s rate limited us; backing off %.0fs", self.provider.name, delay)
            self._after_poll(time.time(), error="rate limited (HTTP 429)")
            return 0
        except AuthError as e:
            self._after_poll(time.time(), error=str(e))
            raise
        except ProviderError as e:
            delay = self.limiter.record_failure()
            log.warning("%s; retrying in %.0fs", e, delay)
            self._after_poll(time.time(), error=str(e))
            return 0

        now = time.time()
        self.limiter.record_success(headers)
        recovered = self.health.record_success(now, used_failover=self.provider.last_failed_addresses > 0)
        if recovered:
            log.info("%s reachable again after %d failed polls (outage %s)",
                     self.provider.name, recovered[0], fmt_duration(recovered[1]))
        sightings = evaluate(self.cfg, aircraft, self.state.aircraft, now, self.limiter.interval_s)
        log.debug("%d aircraft returned, %d alerts", len(aircraft), len(sightings))

        if sightings:
            msg = format_message(sightings, self.provider)
            log.info("ALERT: %s", msg.body.replace("\n", " | "))
            if self.notifier.send_with_retry(msg):
                self.health.record_alerts(len(sightings))
            else:
                # Let it fire again next poll rather than silently swallowing it.
                for s in sightings:
                    st = self.state.aircraft[s.aircraft.hex]
                    st["last_alert"] = None
                    st["inside"] = False

        self.state.prune(now)
        self._after_poll(now)
        return len(sightings)

    def _after_poll(self, now: float, error: str | None = None) -> None:
        """Health bookkeeping, hourly status line, heartbeat, then persist."""
        if error:
            self.health.record_failure(now, error)
        if self.health.hourly_due(now):
            log.info("%s", self.health.hourly_line(now))
        if self.health.heartbeat_due(now, self.cfg.heartbeat_s):
            msg = self.health.heartbeat_message(now, self.cfg.heartbeat_s)
            if self.notifier.send_with_retry(msg):
                log.info("heartbeat sent: %s", msg.body.replace("\n", " | "))
                self.health.heartbeat_sent(now)
            else:
                self.health.heartbeat_failed(now, self.cfg.heartbeat_s)
        self.state.save()

    def run(self) -> None:
        log.info(
            "watching for %s within %.0f mi via %s, polling every %.0fs",
            self.cfg.aircraft_type, self.cfg.radius_miles, self.provider.name, self.limiter.interval_s,
        )
        while not self._stop.is_set():
            try:
                self.poll_once()
            except BudgetExhausted as e:
                log.warning("%s", e)
                self._stop.wait(min(e.wait_s, 3600))
            except AuthError as e:
                # Don't exit: under Docker's restart policy that becomes a tight
                # restart loop hammering the API. Wait, then try again.
                log.error("%s; retrying in 1h", e)
                self._stop.wait(3600)
        self.state.save()
