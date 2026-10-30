"""Every tool, and the allowlist of each agent: built once, checked once."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.agents.tools.base import ZoryTool, aliased_fields, identifier_fields
from app.core.exceptions import ConfigurationError
from app.schemas.agent_persona import AgentPersona


class ToolRegistry:
    """The closed set of tools a turn may call.

    Built at startup. A tool whose arguments could name a record, or two tools
    with one name, is a configuration error and stops the service: an unsafe
    tool must never be offered to a model at all.
    """

    def __init__(self, tools: Iterable[ZoryTool[Any]]) -> None:
        self._tools: dict[str, ZoryTool[Any]] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ConfigurationError(reason="duplicate tool", tool=tool.name)
            unsafe = identifier_fields(tool.args.model_json_schema()) + aliased_fields(tool.args)
            if unsafe:
                raise ConfigurationError(
                    reason="tool arguments name a record", tool=tool.name, fields=unsafe
                )
            self._tools[tool.name] = tool

    def get(self, name: str) -> ZoryTool[Any] | None:
        return self._tools.get(name)

    def for_agent(self, agent: AgentPersona) -> tuple[ZoryTool[Any], ...]:
        return tuple(t for t in self._tools.values() if agent in t.owners)

    def names(self) -> frozenset[str]:
        return frozenset(self._tools)
