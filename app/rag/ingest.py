"""Turn POI records into retrievable documents.

Each description is short (one paragraph), so one POI = one chunk; splitting
would only separate a sentence from the place it describes. The embedded text
adds name, area, category and tags so short queries ("venetian fortress")
match on metadata words as well as the prose.
"""

import hashlib
import json

from pydantic import BaseModel

from app.models import POI


class Document(BaseModel):
    poi_id: str
    name: str
    text: str  # what gets embedded
    passage: str  # what is shown to the LLM as evidence
    source_urls: list[str]


def to_document(poi: POI) -> Document:
    tags = ", ".join(t.replace("_", " ") for t in poi.tags)
    text = (
        f"{poi.name}. Area: {poi.area}. Category: {poi.category.value.replace('_', ' ')}. "
        f"Tags: {tags}. Setting: {poi.setting.value}.\n{poi.description}"
    )
    return Document(poi_id=poi.id, name=poi.name, text=text, passage=poi.description, source_urls=poi.source_urls)


def prepare(pois: list[POI]) -> list[Document]:
    return [to_document(p) for p in pois]


def fingerprint(docs: list[Document], model_name: str) -> str:
    """Changes whenever the corpus or the embedding model changes, so a stale
    index on disk is rebuilt instead of silently used."""
    payload = json.dumps([model_name, [(d.poi_id, d.text) for d in docs]], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
