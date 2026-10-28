"""Knowledge base ingestion: Markdown articles → chunks → embeddings → store.

Runs at startup. Articles whose content hash did not change are skipped, so restarts do
not re-embed everything; articles removed from the folder are deleted from the store.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from core.kb.chunking import chunk_article, load_articles
from core.kb.embeddings import Embedder
from core.kb.store import KnowledgeBaseStore

log = logging.getLogger(__name__)


@dataclass
class IngestReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    chunks: int = 0


async def ingest_knowledge_base(
    folder: Path, store: KnowledgeBaseStore, embedder: Embedder, *, base_url: str
) -> IngestReport:
    report = IngestReport()
    articles = load_articles(folder, base_url)
    if not articles:
        log.warning("knowledge base: no articles found in %s", folder)
    existing = await store.article_hashes()

    seen: set[str] = set()
    for article in articles:
        if article.article_id in seen:
            log.warning("knowledge base: duplicate article id %s, skipped", article.article_id)
            continue
        seen.add(article.article_id)
        if existing.get(article.article_id) == article.content_hash:
            report.unchanged.append(article.article_id)
            continue
        chunks = chunk_article(article)
        embeddings = await embedder.embed_documents([c.text for c in chunks]) if chunks else []
        await store.replace_article(article.article_id, article.content_hash, chunks, embeddings)
        report.chunks += len(chunks)
        (report.updated if article.article_id in existing else report.added).append(article.article_id)

    removed = sorted(set(existing) - seen)
    if removed:
        await store.delete_articles(removed)
        report.removed = removed
    log.info(
        "knowledge base: %d added, %d updated, %d unchanged, %d removed (%d chunks embedded)",
        len(report.added),
        len(report.updated),
        len(report.unchanged),
        len(report.removed),
        report.chunks,
    )
    return report
