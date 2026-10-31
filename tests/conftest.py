from datetime import date

import pytest

from app.data import POIRepository, get_repository

MON = date(2026, 10, 5)
TUE = date(2026, 10, 6)
WED = date(2026, 10, 7)


@pytest.fixture(scope="session")
def repo() -> POIRepository:
    return get_repository()


@pytest.fixture(scope="session")
def poi(repo):
    def _get(poi_id: str):
        p = repo.get(poi_id)
        assert p is not None, poi_id
        return p

    return _get
