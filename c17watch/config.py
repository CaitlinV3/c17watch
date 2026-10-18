"""Config loading and validation. Secrets come from environment variables only."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    pass


@dataclass
class Config:
    lat: float
    lon: float
    radius_miles: float
    cooldown_s: float
    lookahead: bool
    ignore_on_ground: bool
    max_position_age_s: float
    aircraft_type: str
    respect_privacy_flags: bool
    provider: str
    poll_interval_s: float
    max_backoff_s: float
    contact: str | None
    notify: dict[str, Any]
    heartbeat_s: float
    state_path: Path


def _env_float(name: str) -> float | None:
    v = os.environ.get(name)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError as e:
        raise ConfigError(f"{name} must be a number") from e


def load_config(path: str | Path) -> Config:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {path} (copy config.example.toml)") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {path}: {e}") from e

    loc = raw.get("location", {})
    alert = raw.get("alert", {})
    prov = raw.get("provider", {})
    privacy = raw.get("privacy", {})

    # Env vars override the file so you can keep your home location out of it.
    lat = _env_float("C17WATCH_LAT")
    lon = _env_float("C17WATCH_LON")
    lat = lat if lat is not None else loc.get("lat")
    lon = lon if lon is not None else loc.get("lon")
    if lat is None or lon is None:
        raise ConfigError("location not set: add [location] lat/lon or C17WATCH_LAT/C17WATCH_LON")

    cfg = Config(
        lat=float(lat),
        lon=float(lon),
        radius_miles=float(alert.get("radius_miles", 15)),
        cooldown_s=float(alert.get("cooldown_minutes", 30)) * 60,
        lookahead=bool(alert.get("lookahead", True)),
        ignore_on_ground=bool(alert.get("ignore_on_ground", True)),
        max_position_age_s=float(alert.get("max_position_age_s", 60)),
        aircraft_type=str(alert.get("aircraft_type", "C17")).upper(),
        respect_privacy_flags=bool(privacy.get("respect_privacy_flags", True)),
        provider=str(prov.get("name", "adsblol")),
        poll_interval_s=float(prov.get("poll_interval_s", 60)),
        max_backoff_s=float(prov.get("max_backoff_minutes", 5)) * 60,
        contact=prov.get("contact") or None,
        notify=raw.get("notify", {}),
        heartbeat_s=float(raw.get("notify", {}).get("heartbeat_hours", 24)) * 3600,
        state_path=Path(os.environ.get("C17WATCH_STATE_PATH")
                        or raw.get("state", {}).get("path", "c17watch_state.json")),
    )
    _validate(cfg)
    return cfg


def _validate(c: Config) -> None:
    if not -90 <= c.lat <= 90 or not -180 <= c.lon <= 180:
        raise ConfigError("lat must be -90..90 and lon -180..180")
    if not 1 <= c.radius_miles <= 100:
        raise ConfigError("radius_miles must be between 1 and 100")
    if c.cooldown_s < 60:
        raise ConfigError("cooldown_minutes must be at least 1")
    if c.poll_interval_s < 10:
        raise ConfigError("poll_interval_s must be at least 10 (be kind to free APIs)")
    if not 1 * 60 <= c.max_backoff_s <= 60 * 60:
        raise ConfigError("max_backoff_minutes must be between 1 and 60")
    if c.heartbeat_s and c.heartbeat_s < 3600:
        raise ConfigError("heartbeat_hours must be 0 (off) or at least 1")
    if not c.aircraft_type.isalnum() or len(c.aircraft_type) > 4:
        raise ConfigError("aircraft_type must be an ICAO type designator like C17")
