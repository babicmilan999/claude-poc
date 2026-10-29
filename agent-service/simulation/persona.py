"""The simulated customer: a `simulator`-alias LLM playing a bank customer (architecture
section 5).

It only knows what a real customer would know: the `CustomerView` of an incident (persona
hints and the customer's own knowledge). The type system keeps the answer key out:
nothing in this module accepts an `Incident` or an `AnswerKey`, and no model here has a
field for one.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, ConfigDict, Field

from core.llm import LLMProvider, ModelAlias, StructuredOutputError, invoke_structured
from core.masking import mask_text

log = logging.getLogger(__name__)

PROMPT_VERSION = "simulated-customer-v1"

Level = Literal["low", "medium", "high"]


class PersonaSpec(BaseModel):
    """How the simulated customer behaves (`Persona` in `contracts/scenario.schema.json`)."""

    model_config = ConfigDict(extra="forbid")

    vagueness: Level = "medium"
    patience: Level = "medium"
    tone: str | None = None
    phrasing: list[str] = Field(default_factory=list)
    knows: list[str]
    max_turns: int = Field(default=6, ge=1, le=20)


class CustomerView(BaseModel):
    """What the simulated customer is given: who they are and what they know."""

    model_config = ConfigDict(frozen=True)

    customer_id: uuid.UUID
    customer_name: str | None = None
    persona: PersonaSpec
    complaint: str | None = None  # a suggested way to put it (also the email text)


class CustomerTurn(BaseModel):
    """Your next move in the chat with the bank's assistant."""

    message: str = Field(
        default="",
        description="What you type next, as the customer. Empty when you end the conversation.",
    )
    done: bool = Field(
        default=False,
        description="True when you end the conversation now instead of writing a message.",
    )
    satisfied: bool = Field(
        default=False,
        description="When done: true if your problem was explained and you know how to fix it, "
        "or a person is now handling it.",
    )


_VAGUENESS = {
    "low": "You describe the problem precisely, with the details you know.",
    "medium": "You describe the problem in everyday words and leave out some details until asked.",
    "high": "You are vague: you describe what you noticed, not what kind of payment it was, "
    "and you may use the wrong words for it.",
}
_PATIENCE = {
    "low": "You are impatient. If the assistant has not helped after two replies, you may ask to speak to a person.",
    "medium": "You are reasonably patient but want an answer within a few messages.",
    "high": "You are patient and happy to answer the assistant's questions.",
}

SYSTEM = """\
You are role-playing a customer of a bank, chatting with the bank's in-app assistant
about a problem with a payment or a card. Stay in character as the customer at all times.

Who you are{name_part}.
{vagueness}
{patience}
{tone}

What you know (you know nothing else about the problem):
{knows}

How you might put it:
{phrasing}

Rules:
- Write short, natural chat messages (one to three sentences), as a real customer would.
- Share only what you know. You do not know why it happened or any internal bank codes.
  If the assistant asks for something you do not know, say so.
- Never type card numbers, account numbers or passwords.
- End the conversation (done = true, empty message) when the assistant has explained
  what happened and how you can fix it (satisfied = true), or when you give up
  (satisfied = false).
"""

USER = """\
The conversation so far:
{transcript}

Write your next message, or end the conversation."""

FIRST_USER = "The conversation has not started. Write your first message to the assistant."


def _bullets(items: Sequence[str], empty: str) -> str:
    return "\n".join(f"- {mask_text(i)}" for i in items) if items else f"- {empty}"


def system_prompt(view: CustomerView) -> str:
    persona = view.persona
    first_name = (view.customer_name or "").split(" ")[0]
    return SYSTEM.format(
        name_part=f": {first_name}" if first_name else ": a customer",
        vagueness=_VAGUENESS[persona.vagueness],
        patience=_PATIENCE[persona.patience],
        tone=f"Your mood: {persona.tone}." if persona.tone else "",
        knows=_bullets(persona.knows, "only that something went wrong with a payment"),
        phrasing=_bullets(persona.phrasing, "in your own words"),
    )


@dataclass(frozen=True)
class ChatLine:
    role: Literal["customer", "assistant"]
    text: str


def transcript_text(lines: Sequence[ChatLine]) -> str:
    return "\n".join(f"{'You' if line.role == 'customer' else 'Assistant'}: {line.text}" for line in lines)


class SimulatedCustomer:
    """One simulated customer for one conversation."""

    def __init__(self, llm: LLMProvider, view: CustomerView) -> None:
        self._llm = llm
        self.view = view

    async def next_turn(self, lines: Sequence[ChatLine], config: RunnableConfig | None = None) -> CustomerTurn:
        messages = [
            SystemMessage(content=system_prompt(self.view)),
            HumanMessage(content=USER.format(transcript=transcript_text(lines)) if lines else FIRST_USER),
        ]
        try:
            turn = await invoke_structured(self._llm.get(ModelAlias.SIMULATOR), CustomerTurn, messages, config)
        except StructuredOutputError as exc:
            log.warning("simulated customer: unusable model output (%s); ending the conversation", exc)
            return CustomerTurn(done=True, satisfied=False)
        message = turn.message.strip()
        if not message and not turn.done:
            return CustomerTurn(done=True, satisfied=turn.satisfied)
        if turn.done:
            return CustomerTurn(done=True, satisfied=turn.satisfied)
        return CustomerTurn(message=message[:4000])
