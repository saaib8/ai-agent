"""What a tool is: typed arguments, the agents allowed to call it, and code.

The model sees a name, a description and the arguments' schema. It never sees
the handler, the services behind it or the request's scope, and it can never
name a store, a product id or a session: every argument model is checked for
such fields when the registry is built (CLAUDE.md 3.4, 8, 20.2).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict

from app.agents.context import ToolContext
from app.agents.work import ToolStatus, TurnWork
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import AgentStateV1

ArgsT = TypeVar("ArgsT", bound=BaseModel)


class ToolKind(StrEnum):
    ACTION = "action"
    """Runs a service and may change the staged state."""
    RESPOND = "respond"
    """Ends the agent's hop with the reply to the customer."""
    HAND_BACK = "hand_back"
    """Ends the agent's hop and returns the turn to the supervisor."""


class ToolArgs(BaseModel):
    """The base every tool's arguments extend: closed, and immutable."""

    model_config = ConfigDict(frozen=True, extra="forbid")


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """What a handler did: what the model is told, and what the turn keeps."""

    status: ToolStatus = ToolStatus.OK
    code: str | None = None
    """A stable reason the model can act on - `no_products`, `cart_full`."""
    facts: Mapping[str, object] | None = None
    """What the model is told. Plain values; Decimals and enums are turned
    into the text they are shown as before anyone reads them."""
    state: AgentStateV1 | None = None
    """The staged state after the call, when it changed."""
    payload: BaseModel | None = None


Handler = Callable[[ArgsT, ToolContext, TurnWork], Awaitable[ToolOutcome]]


@dataclass(frozen=True, slots=True)
class ZoryTool[ToolArgsT: BaseModel]:
    name: str
    description: str
    args: type[ToolArgsT]
    owners: frozenset[AgentPersona]
    kind: ToolKind = ToolKind.ACTION
    handler: Handler[ToolArgsT] | None = None
    """None only for `respond` and `hand_back`, which the loop handles."""

    def __post_init__(self) -> None:
        if (self.handler is None) != (self.kind is not ToolKind.ACTION):
            raise ValueError(f"tool {self.name}: only an action has a handler")

    def openai_schema(self) -> dict[str, Any]:
        """The function the model is offered."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": _tags_required(self.args.model_json_schema()),
                # Validated here, with one correction - never the provider's
                # strict mode, which these optional fields would fail.
                "strict": False,
            },
        }


def _tags_required(schema: dict[str, Any]) -> dict[str, Any]:
    """Every tagged union member's `kind` marked required, so a model is told
    to write it - a reference without its tag is refused, and leaving it out
    would spend the turn's one correction."""
    properties = schema.get("properties")
    if isinstance(properties, dict):
        kind = properties.get("kind")
        if isinstance(kind, dict) and ("const" in kind or "enum" in kind):
            required = list(schema.get("required", []))
            if "kind" not in required:
                schema["required"] = [*required, "kind"]
    for value in schema.values():
        if isinstance(value, dict):
            _tags_required(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _tags_required(item)
    return schema


_FORBIDDEN_NAMES = frozenset({"id", "ids", "uuid", "sku", "url", "store", "session", "pinecone"})
_FORBIDDEN_SUFFIXES = ("_id", "_ids", "_uuid", "_sku", "_url")


def _snake(name: str) -> str:
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in name).lstrip("_")


def identifier_fields(schema: Mapping[str, Any], path: str = "") -> tuple[str, ...]:
    """Every place in a JSON schema that would let a model name a record.

    Walks nested objects, array items, unions and `$defs`, so an id hidden a
    level down is found as surely as one at the top. A free-form object - a
    dict whose keys the model chooses - is refused outright: its keys could be
    anything, `store_id` included.
    """
    found: list[str] = []
    extra = schema.get("additionalProperties")
    if extra not in (None, False):
        found.append(f"{path or '<root>'}*")
    for name, child in (schema.get("properties") or {}).items():
        where = f"{path}{name}"
        lowered = _snake(name).lower()
        if lowered in _FORBIDDEN_NAMES or lowered.endswith(_FORBIDDEN_SUFFIXES):
            found.append(where)
        if isinstance(child, Mapping):
            found.extend(identifier_fields(child, f"{where}."))
    for key in ("items", "anyOf", "oneOf", "allOf", "prefixItems"):
        nested = schema.get(key)
        for child in nested if isinstance(nested, list) else [nested]:
            if isinstance(child, Mapping):
                found.extend(identifier_fields(child, path))
    for name, definition in (schema.get("$defs") or {}).items():
        if isinstance(definition, Mapping):
            found.extend(identifier_fields(definition, f"$defs.{name}."))
    return tuple(found)


def aliased_fields(model: type[BaseModel], path: str = "") -> tuple[str, ...]:
    """Fields that accept a name the schema does not show.

    An alias lets `{"store_id": 5}` validate into a field the schema calls
    `ref`, which no schema check can see - so tool arguments carry none.
    """
    found: list[str] = []
    for name, info in model.model_fields.items():
        if info.alias is not None or info.validation_alias is not None:
            found.append(f"{path}{name}")
        for nested in _models_in(info.annotation):
            found.extend(aliased_fields(nested, f"{path}{name}."))
    return tuple(found)


def _models_in(annotation: Any) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return [m for arg in getattr(annotation, "__args__", ()) for m in _models_in(arg)]


def one_of(values: tuple[str, ...]) -> Any:
    """A closed choice of approved values, or free text when there are none."""
    return Literal.__getitem__(values) if values else str


def list_of(item: Any) -> Any:
    return list.__class_getitem__(item)
