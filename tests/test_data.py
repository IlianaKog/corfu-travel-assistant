import pytest

from app.models import Setting


def test_dataset_loads_all_records(repo):
    assert len(repo) == 50
    assert repo.get("CF-001").name == "Old Fortress"
    assert repo.get("CF-999") is None


def test_mixed_setting_is_not_shelter(poi):
    assert poi("CF-001").setting is Setting.MIXED
    assert not poi("CF-001").is_sheltered
    assert poi("CF-017").is_sheltered


@pytest.mark.parametrize(
    "query, expected_id",
    [
        # Scenario C names, as typed by the user
        ("Old Fortress", "CF-001"),
        ("New Fortress", "CF-002"),
        ("Achilleion Palace", "CF-021"),
        ("Mon Repos Museum of Palaiopolis", "CF-022"),
        ("Museum of Asian Art", "CF-017"),
        # paraphrases and partial names
        ("the asian art museum", "CF-017"),
        ("achilleion", "CF-021"),
        ("Liston", "CF-032"),
        ("Saint Spyridon", "CF-004"),
    ],
)
def test_resolve_known_names(repo, query, expected_id):
    resolved = repo.resolve_name(query)
    assert resolved is not None and resolved.id == expected_id


@pytest.mark.parametrize(
    "query",
    ["Corfu Aquarium", "Acropolis", "Venetian Water Park", "Paleokastritsa Monastery", ""],
)
def test_unknown_names_are_not_forced(repo, query):
    assert repo.resolve_name(query) is None


def test_fortress_vs_its_gate_is_not_confused(repo):
    # "Old Fortress" must not resolve to "Gate of the Old Fortress"
    assert repo.resolve_name("Old Fortress").id == "CF-001"
    assert repo.resolve_name("Gate of the Old Fortress").id == "CF-027"
