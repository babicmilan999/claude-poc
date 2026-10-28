"""Knowledge base storage and hybrid search.

Hybrid search = vector similarity + keyword matching, fused with Reciprocal Rank Fusion
(RRF): each list contributes `1 / (RRF_K + rank)` per hit, so a chunk that ranks well in
both wins, and neither score scale has to be calibrated against the other.

`PostgresKnowledgeBaseStore` uses pgvector (cosine distance) and Postgres full-text search.
`InMemoryKnowledgeBaseStore` implements the same idea in Python, for unit tests and for
running without a database.
"""

import math
import re
from collections import Counter
from collections.abc import Hashable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.db.models import KB_SCHEMA, KbChunk
from core.kb.chunking import Chunk

RRF_K = 60
CANDIDATES = 20

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "is",
        "it",
        "its",
        "my",
        "me",
        "of",
        "on",
        "or",
        "so",
        "that",
        "the",
        "their",
        "them",
        "there",
        "this",
        "to",
        "was",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
        "yours",
        "not",
        "no",
        "didn",
        "doesn",
        "don",
        "isn",
        "wasn",
        "won",
        "can't",
    ]
)


class SearchHit(BaseModel):
    article_id: str
    title: str
    section: str | None
    url: str
    text: str
    score: float


class KnowledgeBaseStore(Protocol):
    async def article_hashes(self) -> dict[str, str]: ...

    async def replace_article(
        self, article_id: str, content_hash: str, chunks: Sequence[Chunk], embeddings: Sequence[list[float]]
    ) -> None: ...

    async def delete_articles(self, article_ids: Sequence[str]) -> None: ...

    async def hybrid_search(self, query: str, query_embedding: list[float], k: int) -> list[SearchHit]: ...


def keywords(text_: str) -> list[str]:
    return [w for w in _WORD_RE.findall(text_.lower()) if w not in _STOPWORDS and len(w) > 1]


def rrf_fuse(rankings: Sequence[Sequence[Hashable]], k: int = RRF_K) -> dict[Hashable, float]:
    scores: dict[Hashable, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return scores


# --- in memory -------------------------------------------------------------------------


@dataclass
class _Stored:
    chunk: Chunk
    embedding: list[float]
    terms: Counter[str] = field(default_factory=Counter)


class InMemoryKnowledgeBaseStore:
    def __init__(self) -> None:
        self._chunks: dict[tuple[str, int], _Stored] = {}
        self._hashes: dict[str, str] = {}

    async def article_hashes(self) -> dict[str, str]:
        return dict(self._hashes)

    async def replace_article(
        self, article_id: str, content_hash: str, chunks: Sequence[Chunk], embeddings: Sequence[list[float]]
    ) -> None:
        await self.delete_articles([article_id])
        for chunk, embedding in zip(chunks, embeddings, strict=True):
            self._chunks[(article_id, chunk.chunk_index)] = _Stored(
                chunk=chunk, embedding=embedding, terms=Counter(keywords(chunk.text))
            )
        self._hashes[article_id] = content_hash

    async def delete_articles(self, article_ids: Sequence[str]) -> None:
        ids = set(article_ids)
        for key in [key for key in self._chunks if key[0] in ids]:
            del self._chunks[key]
        for article_id in ids:
            self._hashes.pop(article_id, None)

    def __len__(self) -> int:
        return len(self._chunks)

    async def hybrid_search(self, query: str, query_embedding: list[float], k: int) -> list[SearchHit]:
        if not self._chunks:
            return []
        items = list(self._chunks.items())

        by_vector = sorted(items, key=lambda kv: -_cosine(query_embedding, kv[1].embedding))
        vector_ranking = [key for key, _ in by_vector[:CANDIDATES]]

        terms = set(keywords(query))
        n = len(items)
        df = Counter(t for _, s in items for t in set(s.terms) if t in terms)
        avg_len = sum(sum(s.terms.values()) for _, s in items) / n or 1.0
        bm25: list[tuple[tuple[str, int], float]] = []
        for key, stored in items:
            length = sum(stored.terms.values()) or 1
            score = 0.0
            for term in terms:
                tf = stored.terms.get(term, 0)
                if not tf:
                    continue
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / avg_len))
            if score > 0:
                bm25.append((key, score))
        bm25.sort(key=lambda kv: -kv[1])
        keyword_ranking = [key for key, _ in bm25[:CANDIDATES]]

        fused = rrf_fuse([vector_ranking, keyword_ranking])
        best = sorted(fused.items(), key=lambda kv: -kv[1])[:k]
        return [_hit(self._chunks[key].chunk, score) for key, score in best]  # type: ignore[index]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _hit(chunk: Chunk, score: float) -> SearchHit:
    return SearchHit(
        article_id=chunk.article_id,
        title=chunk.title,
        section=chunk.section,
        url=chunk.url,
        text=chunk.text,
        score=round(score, 6),
    )


# --- Postgres ----------------------------------------------------------------------------

_HYBRID_SQL = text(
    f"""
    WITH vec AS (
        SELECT id, row_number() OVER (ORDER BY embedding <=> CAST(:embedding AS vector)) AS rank
        FROM {KB_SCHEMA}.kb_chunks
        ORDER BY embedding <=> CAST(:embedding AS vector)
        LIMIT :candidates
    ),
    kw AS (
        SELECT id, row_number() OVER (ORDER BY ts_rank_cd(tsv, query) DESC) AS rank
        FROM {KB_SCHEMA}.kb_chunks, to_tsquery('english', :tsquery) AS query
        WHERE :tsquery <> '' AND tsv @@ query
        ORDER BY ts_rank_cd(tsv, query) DESC
        LIMIT :candidates
    ),
    fused AS (
        SELECT COALESCE(vec.id, kw.id) AS id,
               COALESCE(1.0 / (:rrf_k + vec.rank), 0) + COALESCE(1.0 / (:rrf_k + kw.rank), 0) AS score
        FROM vec FULL OUTER JOIN kw ON vec.id = kw.id
    )
    SELECT c.article_id, c.title, c.section, c.url, c.text, fused.score
    FROM fused JOIN {KB_SCHEMA}.kb_chunks c ON c.id = fused.id
    ORDER BY fused.score DESC, c.id
    LIMIT :k
    """
)


def to_tsquery_text(query: str) -> str:
    """OR of the query's keywords, safe for `to_tsquery` (only [a-z0-9] words)."""
    return " | ".join(dict.fromkeys(keywords(query)))


class PostgresKnowledgeBaseStore:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def article_hashes(self) -> dict[str, str]:
        async with self._sessionmaker() as session:
            rows = await session.execute(select(KbChunk.article_id, KbChunk.content_hash).distinct())
            return {article_id: content_hash for article_id, content_hash in rows}

    async def replace_article(
        self, article_id: str, content_hash: str, chunks: Sequence[Chunk], embeddings: Sequence[list[float]]
    ) -> None:
        async with self._sessionmaker.begin() as session:
            await session.execute(delete(KbChunk).where(KbChunk.article_id == article_id))
            session.add_all(
                KbChunk(
                    article_id=article_id,
                    chunk_index=chunk.chunk_index,
                    title=chunk.title,
                    section=chunk.section,
                    url=chunk.url,
                    text=chunk.text,
                    content_hash=content_hash,
                    embedding=embedding,
                )
                for chunk, embedding in zip(chunks, embeddings, strict=True)
            )

    async def delete_articles(self, article_ids: Sequence[str]) -> None:
        if not article_ids:
            return
        async with self._sessionmaker.begin() as session:
            await session.execute(delete(KbChunk).where(KbChunk.article_id.in_(list(article_ids))))

    async def hybrid_search(self, query: str, query_embedding: list[float], k: int) -> list[SearchHit]:
        async with self._sessionmaker() as session:
            rows = await session.execute(
                _HYBRID_SQL,
                {
                    "embedding": "[" + ",".join(f"{x:.7f}" for x in query_embedding) + "]",
                    "tsquery": to_tsquery_text(query),
                    "candidates": CANDIDATES,
                    "rrf_k": RRF_K,
                    "k": k,
                },
            )
            return [
                SearchHit(
                    article_id=r.article_id,
                    title=r.title,
                    section=r.section,
                    url=r.url,
                    text=r.text,
                    score=round(float(r.score), 6),
                )
                for r in rows
            ]
