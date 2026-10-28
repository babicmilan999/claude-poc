"""Tools the agents may call. All read-only.

- `banking_customer`: customer-scoped (assistant); `customer_id` injected from the session.
- `banking_ops`: ops-scoped (copilot investigators); restricted per issue type.
- `knowledge_base`: `search_knowledge_base`.
"""

from langchain_core.tools import BaseTool

from core.banking import BankingClient
from core.kb import KnowledgeBaseSearch
from core.tools.banking_customer import CUSTOMER_TOOL_NAMES, make_customer_tools
from core.tools.banking_ops import OPS_TOOLSETS, make_ops_tools, ops_tools_for
from core.tools.common import Clock, utc_now
from core.tools.knowledge_base import make_kb_tool


def make_assistant_tools(
    banking: BankingClient, customer_id: str, kb: KnowledgeBaseSearch | None, *, clock: Clock = utc_now
) -> list[BaseTool]:
    """Everything the answer agent may call: customer-scoped banking tools + KB search."""
    tools = make_customer_tools(banking, customer_id, clock=clock)
    if kb is not None:
        tools.append(make_kb_tool(kb))
    return tools


__all__ = [
    "CUSTOMER_TOOL_NAMES",
    "OPS_TOOLSETS",
    "make_assistant_tools",
    "make_customer_tools",
    "make_kb_tool",
    "make_ops_tools",
    "ops_tools_for",
]
