"""`search_knowledge_base`: hybrid search over the help articles (read-only)."""

from langchain_core.tools import BaseTool
from pydantic import Field

from core.kb import KnowledgeBaseSearch
from core.tools.common import ToolArgs, make_tool


class SearchKnowledgeBaseArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=500, description="What to look up, in plain words.")
    k: int = Field(default=4, ge=1, le=8, description="How many passages to return.")


def make_kb_tool(kb: KnowledgeBaseSearch) -> BaseTool:
    async def search_knowledge_base(query: str, k: int = 4) -> object:
        hits = await kb.search(query, k)
        return [
            {
                "article_id": h.article_id,
                "title": h.title,
                "section": h.section,
                "url": h.url,
                "text": h.text,
            }
            for h in hits
        ]

    return make_tool(
        name="search_knowledge_base",
        description=(
            "Search the help articles (card declines, card controls, scheduled payments, "
            "Direct Debits, fees, contacting support). Returns passages with the article "
            "title and link; cite the article you use."
        ),
        args_schema=SearchKnowledgeBaseArgs,
        func=search_knowledge_base,
    )
