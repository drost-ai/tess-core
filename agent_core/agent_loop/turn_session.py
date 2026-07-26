from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ..agent_runtime_bindings import (
    assertAgentRuntimeBindings,
    materializeAgentExecutionRuntime,
)
from ..provider_turn_session import createProviderTurnSession


async def run_in_provider_turn_session(
    args: dict[str, Any],
    run: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    *,
    is_cancelled: Callable[[dict[str, Any]], bool],
) -> dict[str, Any]:
    session = args["session"]
    config = args["config"]
    runtime_bindings = args.get("runtime")
    assertAgentRuntimeBindings(runtime_bindings, session.get("id"))
    runtime = materializeAgentExecutionRuntime(runtime_bindings)
    logical_turn_id = f"turn-{uuid.uuid4()}"
    provider_turn_session = createProviderTurnSession(
        logical_turn_id=logical_turn_id,
        session=session,
        config=config,
        runtime=runtime,
        model=args.get("modelOverride") or session["model"],
    )
    try:
        return await run(
            {
                **args,
                "logicalTurnId": logical_turn_id,
                "providerTurnSession": provider_turn_session,
            }
        )
    finally:
        if is_cancelled(runtime):
            await provider_turn_session.cancel("turn_cancelled")
        await provider_turn_session.close()
