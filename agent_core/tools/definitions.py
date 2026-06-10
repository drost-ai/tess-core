from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..contracts.context import ToolContext
from ..contracts.tools import ToolResult


@dataclass
class ToolDefinition:
    name: str
    description: str
    inputSchema: dict[str, Any]
    handler: Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.inputSchema
