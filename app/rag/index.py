"""Embedded vector index: fastembed (local ONNX) + NumPy cosine similarity.

For 50 documents a brute-force dot product is exact and takes microseconds; a
vector database would add a service without improving anything.

Build ahead of time (done in the Docker image):  python -m app.rag.index
"""

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from app import config
from app.data import get_repository
from app.rag.ingest import Document, fingerprint, prepare

log = logging.getLogger(__name__)

# Noise floor only. Cosine scores do NOT separate supported from unsupported
# questions on this corpus ("Corfu aquarium with sharks" scores 0.66 against the
# Byzantine Museum, above genuine matches), so evidence is also checked lexically
# via `unmatched_terms`, and the LLM must confirm a passage actually states the fact.
MIN_EVIDENCE_SCORE = 0.5
LEXICAL_BONUS = 0.02  # per query word matching a record's name/area/category/tags

_STOPWORDS = set(
    "what which where when who whom whose why how that this these those there their them they then than "
    "with without from into onto about above below after before during should would could will shall "
    "have has had does did done doing being been were was are is am can may might must also just only "
    "very much many more most some any each every other such near nearby enjoy like love want visit see "
    "corfu place places thing things good best nice tell know find show give make plan time today tomorrow".split()
)


def _content_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 3 and w not in _STOPWORDS]


def _stem(word: str) -> str:
    """Strip plural/verb endings only; a looser stem makes 'nightclub' match 'nightlife'."""
    for suffix, repl in (("ies", "y"), ("sses", "ss"), ("es", ""), ("s", ""), ("ing", ""), ("ed", "")):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4 and not word.endswith("ss"):
            return word[: -len(suffix)] + repl
    return word


@dataclass(frozen=True)
class Hit:
    doc: Document
    score: float


@dataclass(frozen=True)
class SearchResult:
    hits: list[Hit]
    unmatched_terms: list[str]  # query words that appear nowhere in the corpus


class Embedder:
    def __init__(self, model_name: str = config.EMBEDDING_MODEL, cache_dir: Path = config.EMBEDDING_CACHE_DIR):
        from fastembed import TextEmbedding  # imported lazily: heavy, not needed by planner tests

        self.model_name = model_name
        self._model = TextEmbedding(model_name=model_name, cache_dir=str(cache_dir))

    def passages(self, texts: list[str]) -> np.ndarray:
        return _normalize(np.array(list(self._model.passage_embed(texts)), dtype=np.float32))

    def query(self, text: str) -> np.ndarray:
        return _normalize(np.array(list(self._model.query_embed(text)), dtype=np.float32))[0]


def _normalize(m: np.ndarray) -> np.ndarray:
    return m / np.linalg.norm(m, axis=-1, keepdims=True)


class VectorIndex:
    def __init__(self, docs: list[Document], vectors: np.ndarray, embedder: Embedder):
        if len(docs) != len(vectors):
            raise ValueError("documents and vectors differ in length")
        self.docs = docs
        self.vectors = vectors
        self.embedder = embedder
        self._stems = {_stem(w) for d in docs for w in _content_words(d.text)}
        # name + tag stems per document, for the lexical part of the hybrid score
        self._key_stems = [{_stem(w) for w in _content_words(d.text.split(". Setting:")[0])} for d in docs]

    @classmethod
    def build(cls, docs: list[Document], embedder: Embedder) -> "VectorIndex":
        return cls(docs, embedder.passages([d.text for d in docs]), embedder)

    @classmethod
    def load_or_build(cls, docs: list[Document], embedder: Embedder, path: Path = config.INDEX_PATH) -> "VectorIndex":
        fp = fingerprint(docs, embedder.model_name)
        if path.exists():
            data = np.load(path, allow_pickle=False)
            if str(data["fingerprint"]) == fp:
                return cls(docs, data["vectors"], embedder)
            log.info("index at %s is stale; rebuilding", path)
        index = cls.build(docs, embedder)
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(path, vectors=index.vectors, fingerprint=np.array(fp))
        return index

    def similarities(self, query: str) -> np.ndarray:
        """Cosine similarity plus a small bonus per query word found in a record's
        name, area, category or tags. On this corpus cosine scores are bunched
        (0.745-0.761 for the top six of "Venetian history in Corfu", Old Fortress
        sixth); exact metadata matches separate them."""
        sims = self.vectors @ self.embedder.query(query)
        q = {_stem(w) for w in _content_words(query)}
        if q:
            bonus = np.array([len(q & keys) for keys in self._key_stems], dtype=np.float32)
            sims = sims + LEXICAL_BONUS * bonus
        return sims

    def unmatched_terms(self, query: str) -> list[str]:
        """Content words of the query with no (stemmed) occurrence in any document."""
        return list(dict.fromkeys(w for w in _content_words(query) if _stem(w) not in self._stems))

    def search(self, query: str, k: int = 5, allowed_ids: set[str] | None = None, min_score: float = MIN_EVIDENCE_SCORE) -> SearchResult:
        """Top-k documents for `query`, optionally restricted to `allowed_ids`
        (structured pre-filter), dropping anything below `min_score`."""
        return SearchResult(self._hits(query, k, allowed_ids, min_score), self.unmatched_terms(query))

    def _hits(self, query: str, k: int, allowed_ids: set[str] | None, min_score: float) -> list[Hit]:
        sims = self.similarities(query)
        order = np.argsort(-sims, kind="stable")
        hits = []
        for i in order:
            doc = self.docs[i]
            if allowed_ids is not None and doc.poi_id not in allowed_ids:
                continue
            if sims[i] < min_score:
                break
            hits.append(Hit(doc, round(float(sims[i]), 4)))
            if len(hits) == k:
                break
        return hits

    def semantic_scores(self, query: str) -> dict[str, float]:
        """0..1 relevance per POI for ranking itinerary candidates
        (min-max scaled, so it complements the keyword score)."""
        sims = self.similarities(query)
        lo, hi = float(sims.min()), float(sims.max())
        span = (hi - lo) or 1.0
        return {d.poi_id: (float(s) - lo) / span for d, s in zip(self.docs, sims)}


@lru_cache(maxsize=1)
def get_index() -> VectorIndex:
    return VectorIndex.load_or_build(prepare(get_repository().all()), Embedder())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    idx = get_index()
    print(f"index ready: {len(idx.docs)} documents, dim {idx.vectors.shape[1]}, at {config.INDEX_PATH}")
