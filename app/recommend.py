"""Candidate filtering (hard constraints) and interest scoring (soft ranking).

Hard constraints are metadata filters and are applied directly; they never go
through vector search. Interest scoring combines a small, explicit vocabulary
over the dataset's own tags/categories with an optional semantic score supplied
by the retrieval layer, so unseen wording still ranks sensibly.
"""

import re
from collections.abc import Mapping

from app.itinerary import PlanConstraints
from app.models import POI, Category, Setting

# Maps interest words to dataset tags/categories. Only vocabulary that exists in
# the dataset is used; anything else falls back to text and semantic matching.
INTEREST_VOCAB: dict[str, dict[str, set[str]]] = {
    "history": {"categories": {"history"}, "tags": {"history", "heritage", "venetian", "fortress", "monument", "byzantine", "british"}},
    "museum": {"categories": set(), "tags": {"museum", "gallery"}},
    "art": {"categories": set(), "tags": {"art", "gallery", "painting", "studio"}},
    "venetian": {"categories": set(), "tags": {"venetian", "fortress"}},
    "byzantine": {"categories": set(), "tags": {"byzantine", "icons", "religion"}},
    "british": {"categories": set(), "tags": {"british"}},
    "architecture": {"categories": set(), "tags": {"architecture", "palace", "square"}},
    "religion": {"categories": set(), "tags": {"religion", "church"}},
    "nature": {"categories": {"nature"}, "tags": {"park", "garden", "grove", "green_space", "waterfront"}},
    "view": {"categories": set(), "tags": {"viewpoint", "waterfront"}},
    "food": {"categories": {"food"}, "tags": {"restaurant", "food", "cafe", "lunch"}},
    "shopping": {"categories": {"local_market"}, "tags": {"shopping", "craft", "local_market"}},
    "nightlife": {"categories": {"nightlife"}, "tags": {"bar", "nightlife"}},
    "music": {"categories": set(), "tags": {"music", "theatre", "performance"}},
    "archaeology": {"categories": set(), "tags": {"archaeology", "artemis", "gorgon"}},
    "jewish": {"categories": set(), "tags": {"jewish", "memorial"}},
    "walking": {"categories": set(), "tags": {"walking", "old_town", "square"}},
}

# Word forms and close synonyms that normalise onto a vocabulary key.
_ALIASES = {
    "historical": "history", "historic": "history", "heritage": "history", "fortress": "venetian",
    "fortresses": "venetian", "castle": "venetian", "castles": "venetian", "venice": "venetian",
    "museums": "museum", "gallery": "museum", "galleries": "museum", "exhibition": "museum",
    "arts": "art", "painting": "art", "paintings": "art", "artistic": "art",
    "church": "religion", "churches": "religion", "religious": "religion",
    "park": "nature", "parks": "nature", "garden": "nature", "gardens": "nature", "green": "nature",
    "outdoors": "nature", "outdoor": "nature", "open": "nature", "views": "view", "viewpoint": "view", "scenic": "view", "sea": "view",
    "eat": "food", "lunch": "food", "dinner": "food", "restaurant": "food", "cafe": "food", "coffee": "food",
    "shop": "shopping", "shops": "shopping", "crafts": "shopping", "souvenirs": "shopping",
    "bar": "nightlife", "bars": "nightlife", "drinks": "nightlife",
    "ancient": "archaeology", "archaeological": "archaeology",
    "buildings": "architecture", "palace": "architecture", "palaces": "architecture",
}

# Default when no interests or categories are given: sightseeing, not meals or bars.
DEFAULT_CATEGORIES = {Category.HISTORY, Category.NATURE, Category.LOCAL_MARKET}

# Combined (keyword + min-max scaled semantic) relevance needed to be a candidate.
# At 0.15 the semantic part alone let unrelated fillers (Liston Arcades for
# "art and museums") in; 0.3 keeps only places that match the stated interest.
MIN_RELEVANCE = 0.3


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower())


_SETTING_WORDS = {
    "outdoor": Setting.OUTDOOR, "outdoors": Setting.OUTDOOR, "outside": Setting.OUTDOOR, "open air": Setting.OUTDOOR,
    "open-air": Setting.OUTDOOR, "indoor": Setting.INDOOR, "indoors": Setting.INDOOR, "inside": Setting.INDOOR,
}


def split_setting_words(interests: list[str] | None) -> tuple[list[str] | None, list[Setting]]:
    """'outdoor' is an exposure constraint, not a topic. Move such words from
    interests to settings so they filter instead of skewing relevance,
    whichever field the LLM put them in. 'outdoor historical' -> (['historical'], [outdoor])."""
    if interests is None:
        return None, []
    rest, settings = [], []
    for phrase in interests:
        text = phrase.lower()
        for word, setting in _SETTING_WORDS.items():
            if re.search(rf"\b{re.escape(word)}\b", text):
                settings.append(setting)
                text = re.sub(rf"\b{re.escape(word)}\b", " ", text)
        text = re.sub(r"\s+", " ", text).strip(" ,-")
        if text and text not in {"place", "places", "something", "activity", "activities", "attraction", "attractions", "stuff"}:
            rest.append(text)
    return rest, list(dict.fromkeys(settings))


# Words that name no topic: "historical attractions" is just "history".
_FILLER_WORDS = {"a", "an", "and", "the", "of", "some", "main", "famous", "interesting", "local", "corfu",
                 "attraction", "attractions", "site", "sites", "place", "places", "thing", "things",
                 "spot", "spots", "sight", "sights", "landmark", "landmarks"}


def canonical_interests(interests: list[str]) -> list[str]:
    """Map phrasings that differ only in word form or filler onto vocabulary
    terms, so equivalent requests rank alike. Seen with two models: Gemini sent
    no interests (ranked as "history"), gpt-oss sent "historical", and the
    raw-text parts of the score (embedding, text mentions) chose different short
    stops. A phrase with any word outside the vocabulary ("Gorgon pediment",
    "Byzantine icons") is kept as written, since that detail is what the
    semantic score is for."""
    out = []
    for phrase in interests:
        words = [w for w in _words(phrase) if w not in _FILLER_WORDS]
        keys = [_ALIASES.get(w, w) for w in words]
        if keys and all(k in INTEREST_VOCAB for k in keys):
            out.extend(keys)
        else:
            out.append(phrase)
    return list(dict.fromkeys(out))


def interest_keys(interests: list[str]) -> set[str]:
    keys = set()
    for phrase in interests:
        for w in _words(phrase):
            w = _ALIASES.get(w, w)
            if w in INTEREST_VOCAB:
                keys.add(w)
    return keys


def keyword_scores(pois: list[POI], interests: list[str]) -> dict[str, float]:
    """0..1 relevance from tag/category vocabulary and literal text mentions."""
    keys = interest_keys(interests)
    raw_words = {w for phrase in interests for w in _words(phrase) if len(w) > 3}
    scores = {}
    for poi in pois:
        tags = set(poi.tags)
        text = f"{poi.name} {poi.description}".lower()
        s = 0.0
        for key in keys:
            vocab = INTEREST_VOCAB[key]
            if poi.category.value in vocab["categories"]:
                s += 1.0
            s += min(len(tags & vocab["tags"]), 2) * 0.75
        s += 0.25 * sum(1 for w in raw_words if w in text)
        scores[poi.id] = s
    top = max(scores.values(), default=0.0)
    return {k: (v / top if top else 0.0) for k, v in scores.items()}


def combine_scores(
    keyword: Mapping[str, float], semantic: Mapping[str, float] | None = None, semantic_weight: float = 0.4
) -> dict[str, float]:
    if not semantic:
        return dict(keyword)
    return {
        k: (1 - semantic_weight) * keyword.get(k, 0.0) + semantic_weight * semantic.get(k, 0.0)
        for k in set(keyword) | set(semantic)
    }


def exclusion_reason(poi: POI, c: PlanConstraints) -> str | None:
    """Why a POI is out of scope or violates the hard constraints, or None.
    Opening hours and walking are checked later by the scheduler."""
    if c.categories and poi.category not in c.categories:
        return f"category '{poi.category.value}' not requested"
    if not c.categories and not c.interests and poi.category not in DEFAULT_CATEGORIES:
        return f"category '{poi.category.value}' not part of default sightseeing"
    return constraint_violation(poi, c)


def constraint_violation(poi: POI, c: PlanConstraints) -> str | None:
    """Violations of explicit visitor constraints (avoid X, outdoor only, child),
    independent of which categories the plan focuses on."""
    if poi.id in c.excluded_poi_ids:
        return "excluded by the visitor"
    if poi.category in c.exclude_categories:
        return f"category '{poi.category.value}' excluded"
    excluded_tags = expand_exclusions(c.exclude_tags)
    if hit := excluded_tags & set(poi.tags):
        return f"tagged {', '.join(sorted(hit))}, which the visitor wants to avoid"
    if c.settings and poi.setting not in c.settings:
        return f"setting '{poi.setting.value}' not allowed ({', '.join(s.value for s in c.settings)} only)"
    if c.with_child and poi.family_suitability_fixture is not True:
        return "not confirmed as family-suitable in the dataset"
    return None


def expand_exclusions(tags: list[str]) -> set[str]:
    out = set()
    for t in tags:
        key = _ALIASES.get(t.lower(), t.lower())
        out |= INTEREST_VOCAB.get(key, {}).get("tags", set()) or {t.lower()}
    return out


def candidates(
    pois: list[POI], c: PlanConstraints, scores: Mapping[str, float], min_relevance: float = MIN_RELEVANCE
) -> list[POI]:
    """POIs that pass hard constraints and, if interests were given, are relevant.
    Required POIs are always included so the scheduler can explain them."""
    out = []
    for poi in pois:
        if poi.id in c.required_poi_ids:
            out.append(poi)
            continue
        if exclusion_reason(poi, c):
            continue
        if c.interests and scores.get(poi.id, 0.0) < min_relevance:
            continue
        out.append(poi)
    return out
