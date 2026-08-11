from __future__ import annotations

import copy
import uuid
from typing import Any

from .._utils import maybe_await, merge_usage
from ..storage.message_origin import synthetic_user_message as _synthetic_user_message
from ..providers.prompt_cache_orchestration import (
    attach_prepared_prompt_cache_context,
    commit_provider_prompt_cache_observation,
    create_prompt_cache_turn_context,
)
from ..providers.activity import ProviderActivityEmitter, emit_provider_activity
from ..providers.timeouts import createTurnDeadlineContext
from ..session.persistence import persist_session
from ..context.token_accounting import record_actual_token_usage
from ..providers.usage_costs import estimate_usage_cost_usd, merge_usage_into_session, round_usd
from .cancellation import _is_turn_abort_error, _throw_if_turn_cancelled, _safe_emit
from .context import is_context_window_failure, prepare_provider_request
from .providers import _create_provider, _record_provider_failure_if_needed
from .shared import (
    model_supports_image_inputs,
    request_has_image_inputs,
    unsupported_image_input_response,
)


_EMPTY_RESPONSE_NUDGE = "\n".join(
    (
        "[Continuation required]",
        "The previous provider response was empty: it contained no text and no tool calls.",
        "Continue from the existing transcript now. Either provide the next answer or call the appropriate tool.",
        "[/Continuation required]",
        "",
    )
)


async def request_provider_response(args: dict[str, Any]) -> dict[str, Any]:
    config, runtime, session = args["config"], args["runtime"], args["session"]
    deadlines = createTurnDeadlineContext(
        config=config,
        parentSignal=runtime.get("abortSignal"),
        perCallOptions=args.get("providerRequestOptions") or {},
        runtimeContract=args.get("runtimeContract"),
    )
    logical_turn_id = args.get("logicalTurnId") or f"turn-{uuid.uuid4()}"
    logical_call_id = (
        f"{args.get('logicalTurnId')}-call-"
        f"{max(1, int(args.get('logicalCallIndex') or 1))}"
        if args.get("logicalTurnId")
        else f"call-{uuid.uuid4()}"
    )
    activity = ProviderActivityEmitter(
        emitEvent=runtime.get("emitEvent", lambda *_: None),
        sessionId=session["id"],
        logicalTurnId=logical_turn_id,
        logicalCallId=logical_call_id,
        provider=config.get("provider"),
        profile=config.get("providerProfileName") or config.get("provider"),
        model=args["activeModel"],
        coalesceIntervalMs=int(
            (config.get("providerProfile") or {}).get("providerActivityCoalesceMs")
            or 250
        ),
    )
    deadline_runtime = {
        **runtime,
        "abortSignal": deadlines.signal,
        "providerActivityEmitter": activity,
        "logicalCallId": logical_call_id,
    }
    provider = args.get("providerOverride") or _create_provider(
        config,
        deadline_runtime,
        session,
        args.get("providerTurnSession"),
    )
    turn_context = await create_prompt_cache_turn_context(
        {"config": config, "session": session, "paths": args["paths"]}
    )

    async def prepare(reactive_attempt: int) -> dict[str, Any]:
        emit_provider_activity(
            deadline_runtime,
            "request_preparing",
            attempt=reactive_attempt + 1,
        )
        prepared = await deadlines.race(
            prepare_provider_request(
                {
                    "session": session,
                    "config": config,
                    "runtime": deadline_runtime,
                    "baseSystemPrompt": args["baseSystemPrompt"],
                    "toolRegistry": args["toolRegistry"],
                    "activeModel": args["activeModel"],
                    "prompt": args["prompt"],
                    "reactiveAttempt": reactive_attempt,
                    "providerRequestOptions": args.get("providerRequestOptions") or {},
                    "requestPreflightHints": args.get("requestPreflightHints") or {},
                    "deadlines": deadlines,
                }
            )
        )
        return attach_prepared_prompt_cache_context(
            {
                "prepared": prepared,
                "config": config,
                "runtime": deadline_runtime,
                "session": session,
                "paths": args["paths"],
                "reactiveAttempt": reactive_attempt,
                "repair": args.get("repair"),
                "turnContext": turn_context,
            }
        )

    async def dispatch(prepared: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        while True:
            try:
                _throw_if_turn_cancelled(runtime)
                request = prepared["request"]
                if prepared.get("unsupportedImageInputs") or (
                    request_has_image_inputs(request)
                    and not model_supports_image_inputs(config, args["activeModel"])
                ):
                    response = unsupported_image_input_response(args["activeModel"])
                else:
                    emit_provider_activity(
                        deadline_runtime,
                        "request_started",
                        attempt=int(prepared.get("reactiveAttempt") or 0) + 1,
                    )
                    response = await deadlines.race(maybe_await(provider["send"](request)))
                await commit_provider_prompt_cache_observation(response, deadline_runtime)
                return prepared, response
            except Exception as error:
                if _is_turn_abort_error(error, runtime.get("abortSignal")):
                    raise
                attempt = int(prepared.get("reactiveAttempt") or 0)
                if attempt < 2 and is_context_window_failure(error):
                    emit_provider_activity(
                        deadline_runtime,
                        "retry_wait",
                        attempt=attempt + 1,
                        force=True,
                    )
                    prepared = await prepare(attempt + 1)
                    continue
                raise

    try:
        prepared, response = await dispatch(await prepare(0))
        _throw_if_turn_cancelled(runtime)
        empty_attempts = 0
        empty_cost = 0.0
        while is_empty_assistant_response(response) and empty_attempts < 1:
            empty_attempts += 1
            empty_cost += record_empty_response_usage(
                response=response,
                session=session,
                totalUsage=args["totalUsage"],
                runtime=runtime,
                attempt=empty_attempts,
                retrying=True,
            )
            reactive_attempt = max(int(prepared.get("reactiveAttempt") or 0) + 1, 1)
            prepared = add_empty_response_continuation_nudge(await prepare(reactive_attempt))
            prepared, response = await dispatch(prepared)
            _throw_if_turn_cancelled(runtime)
        result = {
            "preparedRequest": prepared,
            "response": response,
            "emptyResponseAttempts": empty_attempts,
            "emptyResponseCostUsd": empty_cost,
        }
        emit_provider_activity(
            deadline_runtime,
            "terminal",
            attempt=int(prepared.get("reactiveAttempt") or 0) + 1,
            force=True,
        )
        return result
    except Exception as error:
        emit_provider_activity(deadline_runtime, "terminal", force=True)
        await _record_provider_failure_if_needed(session, config, runtime, error)
        raise
    finally:
        deadlines.dispose()


def is_empty_assistant_response(response: dict[str, Any]) -> bool:
    content = response.get("content") if isinstance(response.get("content"), list) else []
    if any(isinstance(block, dict) and block.get("type") == "tool_use" for block in content):
        return False
    texts = [
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return all(not text.strip() for text in texts)


def add_empty_response_continuation_nudge(prepared: dict[str, Any]) -> dict[str, Any]:
    next_prepared = copy.deepcopy(prepared)
    messages = next_prepared["request"].setdefault("messages", [])
    messages.append(
        _synthetic_user_message(
            _EMPTY_RESPONSE_NUDGE,
            kind="empty_response_nudge",
        )
    )
    return next_prepared


def record_empty_response_usage(args: dict[str, Any] | None = None, **kwargs: Any) -> float:
    values = {**(args or {}), **kwargs}
    response, session = values["response"], values["session"]
    usage = response.get("usage") or {}
    merge_usage(values["totalUsage"], usage)
    merge_usage_into_session(session, usage)
    record_actual_token_usage(session, usage)
    _safe_emit(
        values["runtime"],
        "provider.empty_response",
        {
            "session_id": session.get("id"),
            "model": response.get("model"),
            "stop_reason": response.get("stopReason"),
            "attempt": values.get("attempt", 1),
            "retrying": bool(values.get("retrying")),
            **({"final_stop_reason": "empty_response"} if not values.get("retrying") else {}),
        },
    )
    return estimate_usage_cost_usd(usage, response.get("model") or "")


async def complete_empty_provider_response(args: dict[str, Any]) -> dict[str, Any] | None:
    response = args["response"]
    if not is_empty_assistant_response(response):
        return None
    cost = float(args.get("estimatedCostUsd") or 0) + record_empty_response_usage(
        response=response,
        session=args["session"],
        totalUsage=args["totalUsage"],
        runtime=args["runtime"],
        attempt=int(args.get("emptyResponseAttempts") or 0) + 1,
        retrying=False,
    )
    await persist_session(args["session"], args["config"])
    return {
        "text": "\n\n".join(args.get("textParts") or []).strip(),
        "toolCount": int(args.get("toolCount") or 0),
        "usage": args["totalUsage"],
        "model": response.get("model"),
        "estimatedCostUsd": round_usd(cost),
        "stopReason": "empty_response",
    }


