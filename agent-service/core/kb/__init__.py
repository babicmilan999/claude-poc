"""Knowledge base: Markdown articles, chunking, embeddings and hybrid search."""

from dataclasses import dataclass

from core.kb.embeddings import Embedder
from core.kb.store import KnowledgeBaseStore, SearchHit


@dataclass
class KnowledgeBaseSearch:
    store: KnowledgeBaseStore
    embedder: Embedder

    async def search(self, query: str, k: int = 4) -> list[SearchHit]:
        query = query.strip()
        if not query:
            return []
        embedding = await self.embedder.embed_query(query)
        return await self.store.hybrid_search(query, embedding, k)


__all__ = ["KnowledgeBaseSearch", "SearchHit"]
