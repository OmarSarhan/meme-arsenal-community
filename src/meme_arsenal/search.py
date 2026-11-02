from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .embeddings import (cosine_similarity, embed_text, embedding_is_compatible,
                         embedding_source_text, embedding_text_hash, select_embedding_input,
                         validate_embedding_vector)
from .catalog import _latest_classification
from .link_preview import link_preview_text, message_link_preview
from .models import Classification, Curation, Embedding, MediaItem, TelegramMessage


@dataclass
class SearchHit:
    media_item_id: int
    score: float
    semantic_score: float
    lexical_score: float
    explanation: str


def _tokens(query: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[a-zA-Z0-9_+-]{2,}", query)}


def _lexical_blob(
    message: TelegramMessage | None,
    classification: Classification | None,
    media_item: MediaItem,
    curation: Curation | None,
) -> str:
    pieces: list[str] = [media_item.media_type or "", media_item.mime_type or ""]
    if message:
        pieces.append(message.caption or "")
        pieces.append(message.telegram_post_url or "")
        pieces.append(link_preview_text(message_link_preview(message)))
    if classification:
        pieces.extend(
            [
                classification.summary,
                classification.visible_text,
                classification.narration,
                classification.language or "",
                classification.meme_format or "",
                " ".join(classification.tone or []),
                " ".join(classification.reaction_type or []),
                " ".join(classification.topics_json or []),
                " ".join(classification.keywords_json or []),
                " ".join(classification.people_or_characters_json or []),
                " ".join(classification.safety_flags_json or []),
                " ".join(classification.search_phrases_json or []),
            ]
        )
    if curation:
        pieces.extend(["favorite" if curation.favorite else "", "archived" if curation.archived else ""])
        pieces.extend(curation.manual_tags_json or [])
        pieces.append(curation.notes or "")
    return "\n".join(pieces).lower()


def _lexical_scores(session: Session, query: str) -> dict[int, tuple[float, str]]:
    tokens = _tokens(query)
    if not tokens:
        return {}
    statement = select(MediaItem, TelegramMessage, Classification, Curation).join(TelegramMessage).outerjoin(
        Classification, Classification.media_item_id == MediaItem.id
    ).outerjoin(Curation, Curation.media_item_id == MediaItem.id)
    scores: dict[int, tuple[float, str]] = {}
    rows = session.execute(statement).all()
    classifications: dict[int, list[Classification]] = {}
    for media_item, _, classification, _ in rows:
        if classification is not None:
            classifications.setdefault(media_item.id, []).append(classification)
    seen = set()
    for media_item, message, _, curation in rows:
        if media_item.id in seen:
            continue
        seen.add(media_item.id)
        classification = _latest_classification(classifications, media_item.id)
        blob = _lexical_blob(message, classification, media_item, curation)
        matches = sorted(token for token in tokens if token in blob)
        if matches:
            scores[media_item.id] = (len(matches) / len(tokens), f"Matched: {', '.join(matches)}")
    return scores


def _semantic_scores_portable(
    session: Session,
    settings: Settings,
    query_vector: list[float],
    top_k: int,
) -> dict[int, tuple[float, str]]:
    validate_embedding_vector(query_vector, settings.embedding_dimensions)
    scored = []
    with session.no_autoflush:
        for item in session.scalars(select(MediaItem).order_by(MediaItem.id)).all():
            classification = select_embedding_input(session, settings, item)
            if classification is None:
                continue
            text_hash = embedding_text_hash(embedding_source_text(session, item, classification))
            rows = session.scalars(select(Embedding).where(
                Embedding.media_item_id == item.id
            ).order_by(Embedding.created_at.desc(), Embedding.id.desc())).all()
            embedding = next((row for row in rows if embedding_is_compatible(row, settings, text_hash)), None)
            if embedding is not None:
                scored.append((item.id, cosine_similarity(query_vector, embedding.embedding_json)))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return {media_item_id: (score, "Embedding cosine similarity")
            for media_item_id, score in scored[:top_k]}


def search_media(
    session: Session,
    settings: Settings,
    query: str,
    top_k: int | None = None,
) -> list[SearchHit]:
    top_k = top_k or settings.semantic_search_top_k
    lexical = _lexical_scores(session, query)
    query_vector = embed_text(settings, query)
    semantic = _semantic_scores_portable(session, settings, query_vector, top_k * 4)

    ids = set(lexical) | set(semantic)
    hits: list[SearchHit] = []
    for media_item_id in ids:
        lexical_score, lexical_reason = lexical.get(media_item_id, (0.0, ""))
        semantic_score, semantic_reason = semantic.get(media_item_id, (0.0, ""))
        score = semantic_score * 0.7 + lexical_score * 0.3
        explanation = "; ".join(reason for reason in [lexical_reason, semantic_reason] if reason)
        hits.append(
            SearchHit(
                media_item_id=media_item_id,
                score=score,
                semantic_score=semantic_score,
                lexical_score=lexical_score,
                explanation=explanation,
            )
        )
    hits.sort(key=lambda hit: (-hit.score, hit.media_item_id))
    return hits[:top_k]


def export_search_results(
    session: Session,
    settings: Settings,
    query: str,
    top_k: int | None = None,
) -> Path:
    hits = search_media(session, settings, query, top_k)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", query.strip().lower()).strip("-") or "query"
    path = settings.catalog_dir / "search-results" / f"{slug}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "query": query,
        "results": [
            {
                "media_item_id": hit.media_item_id,
                "score": round(hit.score, 6),
                "semantic_score": round(hit.semantic_score, 6),
                "lexical_score": round(hit.lexical_score, 6),
                "explanation": hit.explanation,
            }
            for hit in hits
        ],
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path
