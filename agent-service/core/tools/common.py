"""Building blocks shared by all tools.

Every tool:
- has a Pydantic args schema with `extra="forbid"`, so the model cannot slip in an
  argument the tool does not declare (for example `customer_id` on a customer tool);
- is read-only (it only calls GET endpoints of the banking API or searches the KB);
- masks its result before returning it, because tool results go back to the LLM;
- returns `(content, artifact)`: a JSON string for the model and the same masked data as
  a structured artifact, so a graph can record it as evidence.
"""

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool, ToolException
from pydantic import BaseModel, ConfigDict

from core.banking import BankingError, BankingNotFound
from core.masking import mask_data

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class ToolArgs(BaseModel):
    """Base for tool argument schemas: unknown arguments are rejected."""

    model_config = ConfigDict(extra="forbid")


class NoArgs(ToolArgs):
    pass


def to_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list | tuple):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


class ReadOnlyTool(StructuredTool):
    """A StructuredTool that always validates its arguments.

    LangChain skips validation for tools without parameters, which would silently drop
    an argument the model made up (for example `customer_id`). Here every call is
    validated against the args schema, so unknown arguments are an error the model sees.
    """

    def _to_args_and_kwargs(self, tool_input: str | dict[str, Any], tool_call_id: str | None) -> Any:
        schema = self.args_schema
        if isinstance(tool_input, dict) and isinstance(schema, type) and issubclass(schema, BaseModel):
            schema.model_validate(tool_input)
        return super()._to_args_and_kwargs(tool_input, tool_call_id)


def make_tool(
    *,
    name: str,
    description: str,
    args_schema: type[ToolArgs],
    func: Callable[..., Awaitable[Any]],
) -> BaseTool:
    async def _run(**kwargs: Any) -> tuple[str, Any]:
        try:
            result = await func(**kwargs)
        except BankingNotFound:
            data: Any = {"error": "not_found", "detail": "No such record for this customer."}
            return _dumps(data), data
        except BankingError as exc:
            raise ToolException(f"The banking service is not available right now ({exc}).") from exc
        masked = mask_data(to_jsonable(result))
        return _dumps(masked), masked

    return ReadOnlyTool.from_function(
        coroutine=_run,
        name=name,
        description=description,
        args_schema=args_schema,
        response_format="content_and_artifact",
        handle_tool_error=True,
    )
