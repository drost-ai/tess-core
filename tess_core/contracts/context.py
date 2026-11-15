"""Tool handler context contract.

Producer: ``build_context`` in ``tess_core/agent/loop.py`` (the agent
loop's per-tool-call constructor). Subagent and test harness contexts add the
``taskSupervisor``/``runSubagent``/``tools`` keys.

The context is never serialized, so this TypedDict carries zero wire risk —
it exists purely for IDE completion and typo protection at the most-
dereferenced boundary in the codebase. ``total=False`` because alternate
constructors supply different subsets.
"""

from __future__ import annotations

from typing import Any, Callable, TypedDict


class AskQuestionDetail(TypedDict, total=False):
    """Context sent to the host with a question. For a permission prompt: the tool, mode, summary and reason."""
    kind: str
    tool: str
    mode: str
    summary: str
    reason: str | None
    category: str | None


class ToolContext(TypedDict, total=False):
    monitorContinuationSupported: bool
    cwd: str
    allowedRoots: list[str]
    config: dict[str, Any]            # resolved config (contracts.config.ResolvedConfig)
    session: dict[str, Any]           # session record (contracts.records SessionRecord, step 5)
    interactive: bool
    outputJson: Callable[..., Any]    # event emitter: (event_type, data) -> None
    askQuestion: Callable[..., Any]   # (question, options, detail?) -> answer string
    persist_session: Callable[[], Any]
    updateCwd: Callable[[str], Any]
    getTools: Callable[[], list[Any]]
    daemon: dict[str, Any] | None     # task supervisor daemon client surface
    taskSupervisor: dict[str, Any] | None
    abortSignal: Any                  # cancellation signal object
    reserveImageAttachment: Callable[[dict[str, Any]], Any]
    runSubagent: Callable[..., Any]   # foreground subagent runner callback
    tools: list[Any]
