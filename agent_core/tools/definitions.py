from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..contracts.context import ToolContext


@dataclass
class ToolDefinition:
    name: str
    description: str
    inputSchema: dict[str, Any]
    handler: Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.inputSchema
