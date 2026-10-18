"""Flight-data providers.

All three supported providers return the same readsb / ADS-B Exchange v2 JSON
shape ({"ac": [...]}) so one parser handles them. Each provider also carries
its own published limits and terms, which the rate limiter enforces.

Limits and terms below were checked October 2026. They change; re-check the
provider's own docs before relying on them, especially before publishing.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass, field
from typing import Any

import logging

import requests
from requests.adapters import HTTPAdapter

from . import __version__

log = logging.getLogger(__name__)

# readsb dbFlags bits
DBFLAG_MILITARY = 1
DBFLAG_INTERESTING = 2
DBFLAG_PIA = 4  # FAA Privacy ICAO Address program
DBFLAG_LADD = 8  # FAA Limiting Aircraft Data Displayed program


class ProviderError(Exception):
    """Transient or unexpected failure talking to the provider."""


class RateLimited(ProviderError):
    def __init__(self, retry_after: float | None):
        super().__init__(f"rate limited (retry after {retry_after}s)")
        self.retry_after = retry_after


class AuthError(ProviderError):
    """Bad or missing API key. Not worth retrying."""


@dataclass
class Aircraft:
    hex: str
    type_code: str | None
    callsign: str | None
    registration: str | None
    lat: float | None
    lon: float | None
    alt_baro: int | str | None  # feet, or the string "ground"
    gs: float | None  # knots
    track: float | None  # degrees true
    seen_pos: float | None  # seconds since last position
    db_flags: int = 0

    @property
    def on_ground(self) -> bool:
        return self.alt_baro == "ground"

    @property
    def privacy_flagged(self) -> bool:
        return bool(self.db_flags & (DBFLAG_PIA | DBFLAG_LADD))

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Aircraft":
        def num(key: str) -> float | None:
            v = d.get(key)
            return float(v) if isinstance(v, (int, float)) else None

        callsign = (d.get("flight") or "").strip() or None
        return cls(
            hex=str(d.get("hex", "")).lower().lstrip("~"),
            type_code=(d.get("t") or None),
            callsign=callsign,
            registration=d.get("r") or None,
            lat=num("lat"),
            lon=num("lon"),
            alt_baro=d.get("alt_baro"),
            gs=num("gs"),
            track=num("track"),
            seen_pos=num("seen_pos"),
            db_flags=int(d.get("dbFlags") or 0),
        )


@dataclass
class Provider:
    name: str
    url_template: str  # formatted with {type}
    map_url: str  # formatted with {hex}
    attribution: str
    terms_url: str
    min_interval_s: float  # hard floor between requests
    daily_limit: int | None = None
    monthly_limit: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    # (connect, read): give up fast on a dead server, allow time for the response
    timeout_s: tuple[float, float] = (5.0, 20.0)
    auth_hint: str = "access denied by the provider"
    _preferred_ip: str | None = field(default=None, repr=False)

    def url(self, type_code: str) -> str:
        return self.url_template.format(type=type_code)

    def _get_with_failover(self, session: requests.Session, url: str) -> requests.Response:
        """GET `url`, trying each of the host's IP addresses in turn.

        urllib3 already falls back to the next address when a TCP connect fails,
        but not when the connection opens and the TLS handshake or response then
        hangs or resets. Pinning each IP explicitly covers that case too. TLS is
        still verified against the real hostname (SNI + certificate check), so
        pinning doesn't weaken anything. The last address that worked is tried
        first on the next poll.
        """
        parsed = requests.utils.urlparse(url)
        host, port = parsed.hostname or "", parsed.port or 443
        try:
            ips = list(dict.fromkeys(
                ai[4][0] for ai in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
        except socket.gaierror:
            ips = []
        if len(ips) < 2:  # nothing to fail over to; let requests resolve normally
            return session.get(url, headers=self.headers, timeout=self.timeout_s)

        if self._preferred_ip in ips:
            ips.remove(self._preferred_ip)
            ips.insert(0, self._preferred_ip)

        last_err: Exception | None = None
        for ip in ips:
            ip_netloc = f"[{ip}]" if ":" in ip else ip
            if parsed.port:
                ip_netloc += f":{parsed.port}"
            prefix = f"{parsed.scheme}://{ip_netloc}/"
            current = session.adapters.get(prefix)
            if not (isinstance(current, _PinnedHostAdapter) and current.hostname == host):
                session.mount(prefix, _PinnedHostAdapter(host))  # (re)pin to this hostname
            pinned_url = parsed._replace(netloc=ip_netloc).geturl()
            try:
                resp = session.get(pinned_url, headers={**self.headers, "Host": parsed.netloc},
                                   timeout=self.timeout_s)
            except (requests.ConnectionError, requests.Timeout) as e:
                log.info("%s: %s failed (%s); trying next address", self.name, ip, type(e).__name__)
                last_err = e
                continue
            self._preferred_ip = ip
            return resp
        assert last_err is not None
        raise last_err

    def fetch(self, session: requests.Session, type_code: str) -> tuple[list[Aircraft], dict[str, str]]:
        """Fetch aircraft. Returns (aircraft, response headers).

        Raises RateLimited, AuthError or ProviderError.
        """
        try:
            resp = self._get_with_failover(session, self.url(type_code))
        except requests.exceptions.SSLError as e:
            raise ProviderError(f"{self.name}: TLS/certificate error (proxy or interception?): {e}") from e
        except (requests.ConnectionError, requests.Timeout) as e:
            hint = diagnose_connection(self.url(type_code))
            raise ProviderError(f"{self.name}: could not connect: {hint or e}") from e
        except requests.RequestException as e:
            raise ProviderError(f"{self.name}: request failed: {e}") from e

        if resp.status_code == 429:
            raise RateLimited(_parse_retry_after(resp.headers.get("Retry-After")))
        if resp.status_code in (401, 403):
            raise AuthError(f"{self.name}: HTTP {resp.status_code} ({self.auth_hint})")
        if resp.status_code >= 400:
            raise ProviderError(f"{self.name}: HTTP {resp.status_code}")

        try:
            payload = resp.json()
        except ValueError as e:
            raise ProviderError(f"{self.name}: response was not JSON") from e

        raw = payload.get("ac") or []
        if not isinstance(raw, list):
            raise ProviderError(f"{self.name}: unexpected response shape")
        return [Aircraft.from_json(a) for a in raw if isinstance(a, dict)], dict(resp.headers)


class _PinnedHostAdapter(HTTPAdapter):
    """Connects to whatever IP is in the URL, but does TLS as `hostname`."""

    def __init__(self, hostname: str, **kw):
        self.hostname = hostname
        super().__init__(**kw)

    def init_poolmanager(self, *args, **kw):
        kw["server_hostname"] = self.hostname  # SNI
        kw["assert_hostname"] = self.hostname  # certificate must match the real host
        super().init_poolmanager(*args, **kw)


def diagnose_connection(url: str) -> str | None:
    """Explain the common local causes of a refused/failed connection."""
    host = requests.utils.urlparse(url).hostname or ""
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)}
    except socket.gaierror:
        return f"DNS lookup for {host} failed (offline, or DNS is blocking it)"
    blocked = [a for a in addrs if ipaddress.ip_address(a).is_loopback or ipaddress.ip_address(a).is_unspecified]
    if blocked:
        return (f"{host} resolves to {', '.join(sorted(blocked))}, so a DNS blocker "
                f"(Pi-hole, NextDNS, ad-block profile, VPN filter) is sinkholing it; allowlist {host}")
    return f"{host} resolves to {', '.join(sorted(addrs))} but the connection failed (firewall, VPN or proxy?)"


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form; caller falls back to backoff


def user_agent(contact: str | None) -> str:
    """Identify the app to API operators so they can reach you instead of
    just blocking the IP. `contact` can be an email or a repo URL."""
    base = f"c17watch/{__version__} python-requests"
    return f"{base} (+{contact})" if contact else base


def build_provider(name: str) -> Provider:
    name = name.lower().replace("-", "").replace(".", "").replace("_", "")

    if name == "adsblol":
        # Free, keyless. Rate limits are dynamic/load-based and unpublished, so we
        # self-impose a conservative floor. Data is ODbL: attribution required.
        return Provider(
            name="adsb.lol",
            url_template="https://api.adsb.lol/v2/type/{type}",
            map_url="https://adsb.lol/?icao={hex}",
            attribution="Data: adsb.lol (ODbL 1.0)",
            terms_url="https://github.com/adsblol/api",
            min_interval_s=30,
        )

    if name == "airplaneslive":
        # Non-commercial. Since ~Aug 2026 the API answers non-feeders with HTTP 403,
        # so this only works from an IP that feeds airplanes.live. 1 req/s; 500/day.
        return Provider(
            name="airplanes.live",
            url_template="https://api.airplanes.live/v2/type/{type}",
            map_url="https://globe.airplanes.live/?icao={hex}",
            attribution="Data: airplanes.live",
            terms_url="https://airplanes.live/terms-of-use/",
            min_interval_s=1,
            daily_limit=500,
            auth_hint="airplanes.live now only serves its API to active feeders",
        )

    if name in ("adsbexchange", "adsbx"):
        # Paid RapidAPI Community plan, non-commercial only. No type endpoint, so
        # pull the military list and filter locally.
        key = os.environ.get("ADSBX_RAPIDAPI_KEY")
        if not key:
            raise AuthError("ADSBX_RAPIDAPI_KEY is not set")
        monthly = int(os.environ.get("ADSBX_MONTHLY_QUOTA", "10000"))
        return Provider(
            name="ADS-B Exchange",
            url_template="https://adsbexchange-com1.p.rapidapi.com/v2/mil/",
            map_url="https://globe.adsbexchange.com/?icao={hex}",
            attribution="Data: ADS-B Exchange",
            terms_url="https://www.adsbexchange.com/terms-of-use/",
            min_interval_s=1,
            monthly_limit=monthly,
            auth_hint="check ADSBX_RAPIDAPI_KEY and your RapidAPI subscription",
            headers={
                "X-RapidAPI-Key": key,
                "X-RapidAPI-Host": "adsbexchange-com1.p.rapidapi.com",
            },
        )

    raise ValueError(f"unknown provider '{name}' (use adsblol, airplaneslive or adsbexchange)")
