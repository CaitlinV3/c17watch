"""Distance math. Everything is in statute miles unless noted."""

from __future__ import annotations

import math

EARTH_RADIUS_MI = 3958.7613
KNOTS_TO_MPH = 1.150779


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in statute miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MI * math.asin(min(1.0, math.sqrt(a)))


def closest_approach_miles(
    home_lat: float,
    home_lon: float,
    ac_lat: float,
    ac_lon: float,
    track_deg: float | None,
    gs_knots: float | None,
    horizon_s: float,
) -> float:
    """Closest distance to home if the aircraft holds course and speed for
    `horizon_s` seconds. Uses a local flat-earth projection, which is accurate
    to well under 1% at the tens-of-miles scale this app cares about.

    Returns the current distance when track/speed are unknown.
    """
    now_dist = haversine_miles(home_lat, home_lon, ac_lat, ac_lon)
    if track_deg is None or not gs_knots or horizon_s <= 0:
        return now_dist

    # Aircraft position relative to home, in miles (x = east, y = north).
    mi_per_deg_lat = math.pi * EARTH_RADIUS_MI / 180
    x0 = (ac_lon - home_lon) * mi_per_deg_lat * math.cos(math.radians(home_lat))
    y0 = (ac_lat - home_lat) * mi_per_deg_lat

    speed_mps = gs_knots * KNOTS_TO_MPH / 3600  # miles per second
    vx = speed_mps * math.sin(math.radians(track_deg))
    vy = speed_mps * math.cos(math.radians(track_deg))

    # Minimise |p0 + v t| for t in [0, horizon].
    v2 = vx * vx + vy * vy
    t = 0.0 if v2 == 0 else max(0.0, min(horizon_s, -(x0 * vx + y0 * vy) / v2))
    return math.hypot(x0 + vx * t, y0 + vy * t)


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, 0-360."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def compass_point(deg: float) -> str:
    points = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return points[int((deg + 22.5) // 45) % 8]
