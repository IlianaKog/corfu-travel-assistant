from app.geo import haversine_km, walk_leg
from app.models import Location


def test_haversine_zero_and_known_distance():
    a = Location(latitude=39.6231266, longitude=19.9285412)
    assert haversine_km(a, a) == 0
    # one degree of latitude is ~111 km
    b = Location(latitude=40.6231266, longitude=19.9285412)
    assert 110.5 < haversine_km(a, b) < 111.5


def test_old_town_legs_are_short_and_walkable(poi):
    leg = walk_leg(poi("CF-001").location, poi("CF-002").location)  # Old -> New Fortress
    assert leg.walkable
    assert 1.0 < leg.straight_km < 1.1
    assert 15 <= leg.minutes <= 25


def test_achilleion_is_not_walkable_from_old_town(poi):
    leg = walk_leg(poi("CF-001").location, poi("CF-021").location)
    assert not leg.walkable
    assert leg.straight_km > 6


def test_mon_repos_is_walkable_from_town(poi):
    # Mon Repos Museum -> Museum of Asian Art: ~2.8 km estimated route, within the 3 km limit
    assert walk_leg(poi("CF-022").location, poi("CF-017").location).walkable


def test_slower_speed_takes_longer(poi):
    a, b = poi("CF-001").location, poi("CF-002").location
    assert walk_leg(a, b, speed_kmh=3.5).minutes > walk_leg(a, b, speed_kmh=4.5).minutes
