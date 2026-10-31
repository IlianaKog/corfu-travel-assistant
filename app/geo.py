"""Distance and walking-time estimates between coordinates.

These are straight-line approximations scaled by a detour factor; they ignore
elevation, steps and road crossings. Callers must present results as estimates.
"""

import math
from dataclasses import dataclass

from app import config
from app.models import Location

EARTH_RADIUS_KM = 6371.0088


def haversine_km(a: Location, b: Location) -> float:
    lat1, lon1 = math.radians(a.latitude), math.radians(a.longitude)
    lat2, lon2 = math.radians(b.latitude), math.radians(b.longitude)
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


@dataclass(frozen=True)
class WalkLeg:
    straight_km: float
    route_km: float  # straight-line distance x detour factor
    minutes: int
    walkable: bool


def walk_leg(
    a: Location,
    b: Location,
    speed_kmh: float = config.WALK_SPEED_KMH,
    detour: float = config.DETOUR_FACTOR,
    max_leg_km: float = config.MAX_WALK_LEG_KM,
) -> WalkLeg:
    straight = haversine_km(a, b)
    route = straight * detour
    minutes = math.ceil(route / speed_kmh * 60) if route > 0 else 0
    return WalkLeg(
        straight_km=round(straight, 3),
        route_km=round(route, 3),
        minutes=minutes,
        walkable=route <= max_leg_km,
    )
