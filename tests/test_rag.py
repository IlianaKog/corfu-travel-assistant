"""Retrieval quality on questions answerable from the descriptions, plus the
signals used to refuse unsupported ones. Uses the real local embedding model."""

import numpy as np
import pytest

from app.rag.index import VectorIndex, get_index
from app.rag.ingest import fingerprint, prepare


@pytest.fixture(scope="module")
def index() -> VectorIndex:
    return get_index()


def top_ids(index, query, k=3):
    return [h.doc.poi_id for h in index.search(query, k=k).hits]


@pytest.mark.parametrize(
    "query, expected_id, k",
    [
        ("Where can I see the Gorgon pediment?", "CF-031", 1),
        ("temple of Artemis finds", "CF-031", 1),
        ("I'd like to see Byzantine icons", "CF-029", 1),
        ("What should I see if I enjoy Venetian history?", "CF-002", 3),
        ("Venetian fortifications with a sea moat", "CF-001", 3),
        ("a museum of Chinese and Japanese objects", "CF-017", 3),
        ("fresh fish restaurant", {"CF-042", "CF-044"}, 3),  # either seafood restaurant
    ],
)
def test_grounded_questions_retrieve_the_right_record(index, query, expected_id, k):
    expected = expected_id if isinstance(expected_id, set) else {expected_id}
    assert expected & set(top_ids(index, query, k))


@pytest.mark.parametrize("query", ["Venetian history in Corfu", "What should I see if I enjoy Venetian history?", "venetian fortifications"])
def test_paraphrases_of_venetian_history_find_both_fortresses(index, query):
    """Seen live: the model rephrased the question and Old Fortress fell to 6th."""
    assert {"CF-001", "CF-002"} <= set(top_ids(index, query, 4))


def test_hits_carry_evidence_and_sources(index):
    hit = index.search("Gorgon pediment", k=1).hits[0]
    assert hit.doc.passage and hit.doc.source_urls
    assert "Gorgon" in hit.doc.passage


@pytest.mark.parametrize(
    "query, missing",
    [
        ("Is there a Corfu aquarium with sharks?", {"aquarium", "sharks"}),
        ("Where is the best techno nightclub?", {"techno", "nightclub"}),
    ],
)
def test_unsupported_topics_are_flagged(index, query, missing):
    assert missing <= set(index.search(query).unmatched_terms)


def test_known_limitation_missing_facts_are_not_flagged_lexically(index):
    """'ticket' occurs in dataset disclaimers ("no ticketing feed is supplied"),
    so the lexical check cannot flag a ticket-price question. Missing *facts*
    (prices, bookings, live hours) are handled by the tool layer, which states
    which fields the dataset does not contain."""
    assert "tickets" not in index.search("How much are tickets for the Achilleion?").unmatched_terms


@pytest.mark.parametrize("query", ["Venetian fortresses with views", "museums about Byzantine icons", "gardens and parks"])
def test_supported_query_has_no_unmatched_terms(index, query):
    assert index.search(query).unmatched_terms == []


def test_structured_prefilter_restricts_results(index):
    allowed = {"CF-011", "CF-015", "CF-023"}
    assert {h.doc.poi_id for h in index.search("history", k=5, allowed_ids=allowed).hits} <= allowed


def test_semantic_scores_cover_all_pois_and_are_scaled(index):
    scores = index.semantic_scores("venetian fortress")
    assert len(scores) == 50
    assert min(scores.values()) == 0.0 and max(scores.values()) == 1.0
    assert scores["CF-001"] > scores["CF-024"]  # Old Fortress over a food bar


def test_stale_index_is_rebuilt(tmp_path, repo):
    docs = prepare(repo.all()[:3])
    embedder = get_index().embedder
    path = tmp_path / "idx.npz"
    np.savez(path, vectors=np.zeros((3, 384), dtype=np.float32), fingerprint=np.array("stale"))
    idx = VectorIndex.load_or_build(docs, embedder, path)
    assert np.abs(idx.vectors).sum() > 0  # rebuilt, not the zero matrix
    assert str(np.load(path)["fingerprint"]) == fingerprint(docs, embedder.model_name)
