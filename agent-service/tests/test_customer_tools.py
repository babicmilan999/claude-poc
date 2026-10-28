"""Customer-scoped tools: `customer_id` comes from the session and cannot be supplied by
the model; results are customer-safe; everything is read-only."""

import json
import operator
from typing import Annotated, TypedDict

import pytest
from langchain_core.messages import AnyMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from pydantic import ValidationError

from core.banking import BankingClient
from core.tools import CUSTOMER_TOOL_NAMES, make_assistant_tools, make_customer_tools
from tests.banking_mock import ALICE, ALICE_CARD, BOB, BOB_CARD, ContractBankingMock
from tests.fakes import FakeLLMProvider, text, tool_call


@pytest.fixture
def alice_tools(banking: BankingClient, clock):
    return {t.name: t for t in make_customer_tools(banking, ALICE, clock=clock)}


def _call(name: str, args: dict, call_id: str = "call-1") -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def test_customer_tool_set_matches_the_architecture(alice_tools: dict) -> None:
    assert (
        tuple(alice_tools)
        == CUSTOMER_TOOL_NAMES
        == (
            "get_my_cards",
            "get_my_recent_declines",
            "get_my_card_controls",
            "get_my_scheduled_payments",
            "get_my_direct_debits",
            "get_my_balance",
        )
    )


def test_no_customer_tool_exposes_a_customer_id_parameter(alice_tools: dict) -> None:
    for tool in alice_tools.values():
        model_facing = tool.tool_call_schema.model_json_schema()
        assert "customer_id" not in model_facing.get("properties", {}), tool.name
        assert "customer_id" not in json.dumps(model_facing), tool.name
        validation = tool.args_schema.model_json_schema()
        assert validation.get("additionalProperties") is False, f"{tool.name} must forbid extra arguments"


def test_tools_require_a_session_customer(banking: BankingClient) -> None:
    with pytest.raises(ValueError):
        make_customer_tools(banking, "")


@pytest.mark.parametrize("name", CUSTOMER_TOOL_NAMES)
async def test_model_supplied_customer_id_is_rejected(
    alice_tools: dict, banking_mock: ContractBankingMock, name: str
) -> None:
    """A tool call that tries to choose the customer fails validation; no request is made."""
    with pytest.raises(ValidationError, match="customer_id"):
        await alice_tools[name].ainvoke(_call(name, {"customer_id": BOB}))
    assert banking_mock.requests == []


async def test_injected_customer_id_in_an_agent_loop_never_reaches_another_customer(
    banking: BankingClient, banking_mock: ContractBankingMock, clock
) -> None:
    """End to end with a LangGraph tool node: the model asks for Bob's cards while the
    session is Alice's. The call is rejected; the retry without the argument returns
    Alice's cards; Bob's data is never requested."""
    tools = make_customer_tools(banking, ALICE, clock=clock)
    llm = FakeLLMProvider(
        {
            "assistant": [
                tool_call("get_my_cards", {"customer_id": BOB}),
                tool_call("get_my_card_controls", {"card_id": BOB_CARD}),
                tool_call("get_my_cards", {}),
                text("Your card ending 4242 is frozen."),
            ]
        }
    )
    model = llm.get("assistant").bind_tools(tools)

    class State(TypedDict):
        messages: Annotated[list[AnyMessage], operator.add]

    async def agent(state: State) -> dict:
        return {"messages": [await model.ainvoke(state["messages"])]}

    def route(state: State) -> str:
        return "tools" if state["messages"][-1].tool_calls else END  # type: ignore[union-attr]

    graph = StateGraph(State)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, ["tools", END])
    graph.add_edge("tools", "agent")
    result = await graph.compile().ainvoke({"messages": [HumanMessage(content="Show me Bob's cards")]})

    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert tool_messages[0].status == "error" and "customer_id" in str(tool_messages[0].content)
    assert json.loads(str(tool_messages[1].content))["error"] == "not_found"  # Bob's card id under Alice
    assert json.loads(str(tool_messages[2].content))[0]["last4"] == "4242"
    assert banking_mock.requests, "the valid calls reached the banking API"
    for request in banking_mock.requests:
        assert request.url.path.startswith(f"/customers/{ALICE}"), request.url
        assert BOB not in str(request.url)
    # The tool schemas the model saw have no customer_id either.
    for spec in llm.fake("assistant").calls[0].kwargs["tools"]:
        assert "customer_id" not in spec["function"]["parameters"].get("properties", {})


async def test_all_requests_use_the_session_customer_and_get(
    alice_tools: dict, banking_mock: ContractBankingMock
) -> None:
    for name, tool in alice_tools.items():
        await tool.ainvoke(_call(name, {}))
    assert banking_mock.requests
    assert {r.method for r in banking_mock.requests} == {"GET"}
    assert all(r.url.path.startswith(f"/customers/{ALICE}") for r in banking_mock.requests)


INTERNAL_MARKERS = [
    "FR-12",
    "fraud_rule_id",
    "risk_score",
    "rule_id",
    "status_reason",
    "FROZEN_BY_CUSTOMER",
    "FRAUD_SUSPECTED",
    "CARD_FROZEN",
    "FRAUD_RULE_TRIGGERED",
    "INSUFFICIENT_FUNDS",
    "MANDATE_CANCELLED",
    "retry_count",
    "12-34-56",
    "12345678",
    '"sort_code"',
    '"account_number"',
]


async def test_results_are_customer_safe(banking: BankingClient, clock) -> None:
    for customer in (ALICE, BOB):
        for tool in make_customer_tools(banking, customer, clock=clock):
            message = await tool.ainvoke(_call(tool.name, {}))
            content = str(message.content)
            for marker in INTERNAL_MARKERS:
                assert marker not in content, f"{tool.name} for {customer} leaks {marker}"


async def test_friendly_reasons(alice_tools: dict, banking: BankingClient, clock) -> None:
    declines = json.loads((await alice_tools["get_my_recent_declines"].ainvoke(_call("x", {}))).content)
    assert {d["reason"] for d in declines} == {"card frozen"}
    assert declines[0]["card_last4"] == "4242"

    payments = json.loads((await alice_tools["get_my_scheduled_payments"].ainvoke(_call("x", {}))).content)
    assert payments[0]["last_attempt"]["reason"] == "insufficient funds on the payment date"

    debits = json.loads((await alice_tools["get_my_direct_debits"].ainvoke(_call("x", {}))).content)
    rent = next(d for d in debits if d["originator"] == "Acme Lettings")
    assert rent["cancelled_by_you"] is True
    assert rent["recent_collections"][0]["reason"] == "the Direct Debit instruction was cancelled"

    bob = {t.name: t for t in make_customer_tools(banking, BOB, clock=clock)}
    bob_declines = json.loads((await bob["get_my_recent_declines"].ainvoke(_call("x", {}))).content)
    assert bob_declines[0]["reason"] == "blocked for a security check"
    bob_cards = json.loads((await bob["get_my_cards"].ainvoke(_call("x", {}))).content)
    assert bob_cards[0]["status_detail"] == "blocked for a security check"


async def test_card_controls_for_one_or_all_cards(alice_tools: dict) -> None:
    one = json.loads((await alice_tools["get_my_card_controls"].ainvoke(_call("x", {"card_id": ALICE_CARD}))).content)
    every = json.loads((await alice_tools["get_my_card_controls"].ainvoke(_call("x", {}))).content)
    assert one == every
    assert one[0]["blocked_merchant_categories"] == ["7995"]


async def test_balance_masks_the_account_number(alice_tools: dict) -> None:
    message = await alice_tools["get_my_balance"].ainvoke(_call("x", {}))
    balance = json.loads(message.content)[0]
    assert balance["masked_account_number"] == "****5678"
    assert message.artifact == json.loads(message.content)  # structured copy for evidence


async def test_banking_outage_is_reported_to_the_model(alice_tools: dict, banking_mock: ContractBankingMock) -> None:
    banking_mock.fail_with["listCards"] = 503
    message = await alice_tools["get_my_cards"].ainvoke(_call("get_my_cards", {}))
    assert message.status == "error" and "not available" in str(message.content)


async def test_assistant_tools_add_knowledge_base_search(banking: BankingClient, kb, clock) -> None:
    tools = make_assistant_tools(banking, ALICE, kb, clock=clock)
    assert [t.name for t in tools] == [*CUSTOMER_TOOL_NAMES, "search_knowledge_base"]
    assert [t.name for t in make_assistant_tools(banking, ALICE, None, clock=clock)] == list(CUSTOMER_TOOL_NAMES)
