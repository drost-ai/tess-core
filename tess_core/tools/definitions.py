from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, TypedDict

from ..contracts.context import ToolContext
from ..contracts.tools import ToolResult


ToolConcurrency = Literal["parallel", "exclusive"]


class McpToolAnnotations(TypedDict, total=False):
    title: str
    readOnlyHint: bool
    destructiveHint: bool
    idempotentHint: bool
    openWorldHint: bool


class McpToolIdentity(TypedDict, total=False):
    server: str
    name: str
    annotations: McpToolAnnotations


@dataclass
class ToolDefinition:
    name: str
    description: str
    inputSchema: dict[str, Any]
    handler: Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]
    # Missing and invalid declarations are deliberately resolved as exclusive
    # by the dispatch scheduler.  This keeps older custom/plugin tools safe.
    concurrency: ToolConcurrency | None = None
    # Internal provenance for definitions generated from MCP discovery.  This
    # is policy input, not model-supplied identity.
    mcp: McpToolIdentity | None = None
    # The most characters a result of this tool may carry inline; a larger
    # result is saved and the model reads it with Read. None takes the default
    # for every tool. 0 is for a tool that bounds its own results, as Read does
    # by paging: it is held only to the runtime's absolute ceiling.
    maxResultChars: int | None = None

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.inputSchema
