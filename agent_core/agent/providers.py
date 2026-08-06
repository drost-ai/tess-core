from __future__ import annotations

import copy
from ..providers.anthropic import sendAnthropicMessagesRequest
from ..providers.errors import isProviderRequestError
from ..providers.openai import sendOpenAiRequest
from ..session.persistence import persistProviderFailure, persistSession
from .cancellation import _safe_emit
from typing import Any
from ..providers.partial_output_state import (
    createPartialOutputStateStore as _create_partial_output_state_store,
    finalizePartialOutputState as _finalize_partial_output_state,
)
from ..providers.timeouts import resolveProviderTimeoutPolicy
from ..runtime.secret_provider import resolveProviderConfigSecrets


async def _record_provider_failure_if_needed(
    session: dict[str, Any],
    config: dict[str, Any],
    runtime: dict[str, Any],
    error: BaseException,
) -> None:
    if not isProviderRequestError(error):
        return
    metadata = session.setdefault("metadata", {})
    if metadata.get("lastProviderFailure") == error.record:
        return
    metadata["lastProviderFailure"] = copy.deepcopy(error.record)
    _safe_emit(runtime, "provider.failed", {"session_id": session["id"], **error.record})
    await persistProviderFailure(session, config, error.record)


def _emit_provider_response_completed(
    *,
    runtime: dict[str, Any],
    session: dict[str, Any],
    config: dict[str, Any],
    response: dict[str, Any],
    prepared_request: dict[str, Any],
    call_index: int,
) -> None:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    token_tracking = metadata.get("tokenTracking") if isinstance(metadata.get("tokenTracking"), dict) else {}
    preflight = token_tracking.get("lastPreflight") if isinstance(token_tracking.get("lastPreflight"), dict) else {}
    runtime_restore = metadata.get("runtimeRestore") if isinstance(metadata.get("runtimeRestore"), dict) else {}
    continuity = prepared_request.get("continuity") if isinstance(prepared_request, dict) else {}
    relief = continuity.get("relief") if isinstance(continuity, dict) and isinstance(continuity.get("relief"), dict) else {}
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    provider_telemetry = (
        response.get("_providerTelemetry")
        if isinstance(response.get("_providerTelemetry"), dict)
        else {}
    )
    _safe_emit(
        runtime,
        "provider.response_completed",
        {
            "session_id": session.get("id"),
            "provider": config.get("provider"),
            "provider_profile": session.get("providerProfileName") or config.get("providerProfileName"),
            "model": response.get("model"),
            "call_index": max(1, int(call_index or 1)),
            "usage": copy.deepcopy(usage),
            "stop_reason": response.get("stopReason"),
            "request_estimate_tokens": preflight.get("inputTokens"),
            "request_estimate_source": preflight.get("source"),
            "context_relief_mode": runtime_restore.get("lastContextReliefMode") or relief.get("selectedMode"),
            "context_relief_pressure": runtime_restore.get("lastContextReliefPressureLevel") or relief.get("pressureLevel"),
            "context_relief_kind": runtime_restore.get("lastContextReliefKind"),
            "context_relief_projection_hash": runtime_restore.get("lastContextReliefProjectionHash"),
            "context_relief_escalation_requested": bool(runtime_restore.get("contextReliefEscalationRequested")),
            **copy.deepcopy(provider_telemetry),
        },
    )

def _create_provider(
    config: dict[str, Any],
    runtime: dict[str, Any],
    session: dict[str, Any] | None = None,
    provider_turn_session: Any = None,
) -> dict[str, Any]:
    metadata = session.get("metadata") if isinstance(session, dict) and isinstance(session.get("metadata"), dict) else {}
    timeout_policy = resolveProviderTimeoutPolicy({"config": config, "runtimeContract": metadata.get("subagentRuntimeContract")})
    if config.get("provider") == "openai":
        if not config.get("openaiToken"):
            return {"send": lambda request: _missing_openai_credential_response(config, request)}
        state_store = _partial_output_state_store(config, runtime, session)
        provider_runtime = runtime
        if state_store is not None and runtime.get("partialOutputStateStore") is None:
            provider_runtime = {
                **runtime,
                "partialOutputStateStore": state_store,
            }
        async def send_openai(request: dict[str, Any]) -> dict[str, Any]:
            await _heal_partial_output_commit(session, state_store)
            resolved = await resolveProviderConfigSecrets(config)
            return await sendOpenAiRequest(
                token=resolved["openaiToken"],
                baseUrl=resolved.get("openaiBaseUrl"),
                headers=resolved.get("customHeaders"),
                mode=resolved.get("openaiApiMode"),
                config=resolved,
                request=request,
                requestOptions=(resolved.get("providerProfile") or {}).get("requestOptions") or {},
                runtime=provider_runtime,
                clientFactory=runtime.get("openaiClientFactory") or resolved.get("openaiClientFactory"),
                turnSession=provider_turn_session,
                **timeout_policy,
            )

        return {"send": send_openai}
    if not config.get("apiKey") and not config.get("authToken"):
        return {"send": lambda request: _missing_anthropic_credential_response(config, request)}
    async def send_anthropic(request: dict[str, Any]) -> dict[str, Any]:
        resolved = await resolveProviderConfigSecrets(config)
        return await sendAnthropicMessagesRequest(
            config=resolved,
            request=request,
            runtime=runtime,
            turnSession=provider_turn_session,
            **timeout_policy,
        )

    return {"send": send_anthropic}


async def _persist_partial_output_commit(
    session: dict[str, Any],
    config: dict[str, Any],
    runtime: dict[str, Any],
    response: dict[str, Any],
) -> bool:
    provider_metadata = response.get("provider_metadata")
    receipt = (
        provider_metadata.get("partialOutputCommit")
        if isinstance(provider_metadata, dict)
        else None
    )
    if not isinstance(receipt, dict):
        return False
    session.setdefault("metadata", {})["partialOutputCommit"] = copy.deepcopy(receipt)
    await persistSession(session, config)
    store = _partial_output_state_store(config, runtime, session)
    if store is not None:
        await _finalize_partial_output_state(store, receipt)
    return True


def _partial_output_state_store(
    config: dict[str, Any],
    runtime: dict[str, Any],
    session: dict[str, Any] | None,
) -> Any:
    if runtime.get("partialOutputStateStore") is not None:
        return runtime["partialOutputStateStore"]
    if isinstance(session, dict) and config.get("sessionPersistence", True):
        return _create_partial_output_state_store(session["id"])
    return None


async def _heal_partial_output_commit(
    session: dict[str, Any] | None,
    store: Any,
) -> None:
    metadata = (
        session.get("metadata")
        if isinstance(session, dict) and isinstance(session.get("metadata"), dict)
        else {}
    )
    if store is not None:
        await _finalize_partial_output_state(
            store,
            metadata.get("partialOutputCommit"),
        )

def _missing_openai_credential_response(config: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    env_var = (((config.get("providerProfile") or {}).get("auth") or {}).get("envVar")) or "the required bearer token env var"
    return {
        "content": [
            {
                "type": "text",
                "text": f"No credential is configured for provider profile '{config.get('providerProfileName')}'. Set {env_var}, provide a Codex auth file, or run `tess auth login --openai` for official OpenAI access.",
            }
        ],
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "model": request["model"],
        "stopReason": "end_turn",
    }

def _missing_anthropic_credential_response(config: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    env_var = (((config.get("providerProfile") or {}).get("auth") or {}).get("envVar")) or "the required Anthropic credential env var"
    return {
        "content": [
            {
                "type": "text",
                "text": f"No credential is configured for provider profile '{config.get('providerProfileName')}'. Set {env_var} or run `tess auth login` to enable model-backed responses.",
            }
        ],
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "model": request["model"],
        "stopReason": "end_turn",
    }
