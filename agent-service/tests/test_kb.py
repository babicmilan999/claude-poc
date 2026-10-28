"""Knowledge base: chunking, ingestion, hybrid search and the search tool."""

import json
import shutil
from pathlib import Path

from core.kb import KnowledgeBaseSearch
from core.kb.chunking import chunk_article, load_articles, parse_article
from core.kb.embeddings import EMBEDDING_DIM, HashingEmbedder
from core.kb.ingest import ingest_knowledge_base
from core.kb.store import InMemoryKnowledgeBaseStore, keywords, rrf_fuse, to_tsquery_text
from core.settings import AGENT_SERVICE_DIR
from core.tools import make_kb_tool
from tests.conftest import FIXTURE_KB


def test_parse_article_with_front_matter() -> None:
    path = FIXTURE_KB / "why-cards-get-declined.md"
    article = parse_article(path, path.read_text(), "/help/")
    assert article.article_id == "why-cards-get-declined"
    assert article.title == "Why was my card declined?"
    assert article.url == "/help/why-cards-get-declined"
    assert not article.body.startswith("#")


def test_parse_article_without_front_matter() -> None:
    article = parse_article(Path("Card Controls.md"), "# Card controls\n\nIntro.\n\n## Limits\n\nText.", "/help/")
    assert article.article_id == "card-controls" and article.title == "Card controls"


def test_broken_front_matter_falls_back_to_the_heading() -> None:
    article = parse_article(Path("x.md"), "---\ntitle: [unclosed\n---\n# Real title\n\nBody.", "/help/")
    assert article.title == "Real title" and article.article_id == "x"


def test_chunks_follow_sections_and_carry_the_title() -> None:
    path = FIXTURE_KB / "why-cards-get-declined.md"
    chunks = chunk_article(parse_article(path, path.read_text(), "/help/"))
    assert [c.section for c in chunks] == [
        None,
        "Your card is frozen",
        "Not enough money",
        "Blocked for a security check",
    ]
    assert all(c.text.startswith("Why was my card declined?") for c in chunks)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_long_sections_are_split() -> None:
    body = "# T\n\n## S\n\n" + "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(20))
    chunks = chunk_article(parse_article(Path("t.md"), body, "/h/"), max_chars=500)
    assert len(chunks) > 5
    assert all(len(c.text) <= 500 + len("T — S\n\n") for c in chunks)


def test_readmes_are_skipped() -> None:
    ids = [a.article_id for a in load_articles(FIXTURE_KB, "/help/")]
    assert ids == ["direct-debits", "scheduled-payments", "why-cards-get-declined"]


def test_the_real_knowledge_base_folder_parses() -> None:
    """The articles in knowledge-base/ (written separately) load without errors."""
    articles = load_articles(AGENT_SERVICE_DIR.parent / "knowledge-base", "/help/")
    for article in articles:
        assert article.article_id and article.title
        assert chunk_article(article), f"{article.article_id} has no content"
    assert len({a.article_id for a in articles}) == len(articles), "duplicate article ids"


async def test_ingest_is_incremental(tmp_path: Path) -> None:
    folder = tmp_path / "kb"
    shutil.copytree(FIXTURE_KB, folder)
    store, embedder = InMemoryKnowledgeBaseStore(), HashingEmbedder()

    first = await ingest_knowledge_base(folder, store, embedder, base_url="/help/")
    assert sorted(first.added) == ["direct-debits", "scheduled-payments", "why-cards-get-declined"]
    total = len(store)

    second = await ingest_knowledge_base(folder, store, embedder, base_url="/help/")
    assert second.added == [] and second.chunks == 0 and len(second.unchanged) == 3

    (folder / "direct-debits.md").write_text("# How Direct Debits work\n\nShorter now.\n")
    (folder / "scheduled-payments.md").unlink()
    third = await ingest_knowledge_base(folder, store, embedder, base_url="/help/")
    assert third.updated == ["direct-debits"] and third.removed == ["scheduled-payments"]
    assert len(store) < total
    assert set(await store.article_hashes()) == {"direct-debits", "why-cards-get-declined"}


async def test_ingest_of_missing_folder_is_empty(tmp_path: Path) -> None:
    report = await ingest_knowledge_base(
        tmp_path / "missing", InMemoryKnowledgeBaseStore(), HashingEmbedder(), base_url="/"
    )
    assert report.added == [] and report.chunks == 0


async def test_hybrid_search_finds_the_right_article(kb: KnowledgeBaseSearch) -> None:
    hits = await kb.search("how do I unfreeze my frozen card", k=3)
    assert hits[0].article_id == "why-cards-get-declined" and hits[0].section == "Your card is frozen"
    hits = await kb.search("direct debit mandate cancelled", k=2)
    assert hits[0].article_id == "direct-debits"
    hits = await kb.search("standing order did not go out", k=2)
    assert hits[0].article_id == "scheduled-payments"
    assert await kb.search("   ") == []


def test_rrf_rewards_agreement() -> None:
    scores = rrf_fuse([["a", "b", "c"], ["b", "a", "d"]])
    assert max(scores, key=scores.__getitem__) in {"a", "b"}
    assert scores["a"] == scores["b"] > scores["c"] == scores["d"]


def test_keyword_query_is_safe_for_postgres() -> None:
    assert to_tsquery_text("Why didn't my card's payment go through?! ' OR 1=1 --") == "card | payment | go | through"
    assert keywords("the and of") == []


def test_hashing_embedder_dimensions() -> None:
    import asyncio

    vec = asyncio.run(HashingEmbedder().embed_query("card frozen"))
    assert len(vec) == EMBEDDING_DIM
    assert abs(sum(v * v for v in vec) - 1.0) < 1e-9


async def test_search_tool(kb: KnowledgeBaseSearch) -> None:
    tool = make_kb_tool(kb)
    message = await tool.ainvoke(
        {"name": tool.name, "args": {"query": "unfreeze card", "k": 2}, "id": "c1", "type": "tool_call"}
    )
    results = json.loads(message.content)
    assert len(results) == 2
    assert set(results[0]) == {"article_id", "title", "section", "url", "text"}
    assert results[0]["url"].startswith("/help/")
    schema = tool.args_schema.model_json_schema()
    assert schema["additionalProperties"] is False and "customer_id" not in schema["properties"]
