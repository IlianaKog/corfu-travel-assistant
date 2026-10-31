"""Dataset loader and POI repository."""

import json
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

from app import config
from app.models import POI

_STOPWORDS = {"the", "of", "and", "corfu", "museum", "st", "saint"}


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> set[str]:
    return {t for t in _normalize(text).split() if t not in _STOPWORDS}


@dataclass(frozen=True)
class NameMatch:
    poi: POI
    score: float


class POIRepository:
    def __init__(self, pois: list[POI], notice: str = ""):
        ids = [p.id for p in pois]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate POI ids in dataset")
        self._pois = pois
        self._by_id = {p.id: p for p in pois}
        self.notice = notice

    @classmethod
    def from_file(cls, path: Path = config.DATASET_PATH) -> "POIRepository":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls([POI.model_validate(p) for p in raw["pois"]], notice=raw.get("notice", ""))

    def all(self) -> list[POI]:
        return list(self._pois)

    def get(self, poi_id: str) -> POI | None:
        return self._by_id.get(poi_id)

    def __len__(self) -> int:
        return len(self._pois)

    def match_name(self, query: str, threshold: float = 0.6) -> list[NameMatch]:
        """Resolve a user-supplied place name to dataset records, best first.

        Combines character similarity with token overlap so that partial names
        ("Asian Art museum", "Mon Repos museum") resolve, while names that are not
        in the dataset return nothing instead of a forced guess.
        """
        q_norm, q_tokens = _normalize(query), _tokens(query)
        if not q_norm:
            return []
        matches = []
        for poi in self._pois:
            n_norm, n_tokens = _normalize(poi.name), _tokens(poi.name)
            if q_norm == n_norm:
                matches.append(NameMatch(poi, 1.0))
                continue
            char_sim = SequenceMatcher(None, q_norm, n_norm).ratio()
            overlap = len(q_tokens & n_tokens) / len(q_tokens | n_tokens) if q_tokens and n_tokens else 0.0
            covers = 1.0 if q_tokens and q_tokens <= n_tokens else 0.0
            score = max(char_sim, 0.5 * overlap + 0.5 * covers)
            if score >= threshold:
                matches.append(NameMatch(poi, round(score, 3)))
        return sorted(matches, key=lambda m: m.score, reverse=True)

    def resolve_name(self, query: str) -> POI | None:
        """Single unambiguous match, or None (unknown or ambiguous name)."""
        matches = self.match_name(query)
        if not matches:
            return None
        if matches[0].score == 1.0 or len(matches) == 1 or matches[0].score - matches[1].score >= 0.1:
            return matches[0].poi
        return None


@lru_cache(maxsize=1)
def get_repository() -> POIRepository:
    return POIRepository.from_file()
