from __future__ import annotations

import copy
from ..providers.anthropic import send_anthropic_messages_request
from ..providers.errors import ProviderRequestError, is_provider_request_error
from .._utils import now_iso
from ..providers.google import send_google_interactions_request
from ..providers.google.cache import diagnose_google_cache, google_cache_event
from ..providers.openai import send_openai_request
from ..session.persistence import persist_provider_failure, persist_session
from ..runtime.events.emit import safe_emit as _safe_emit
from typing import Any
from ..providers.partial_output_state import (
    create_partial_output_state_store as _create_partial_output_state_store,
    finalize_partial_output_state as _finalize_partial_output_state,
)
from ..providers.timeouts import resolveProviderTimeoutPolicy
from ..runtime.secret_provider import resolve_provider_config_secrets


async def _record_provider_failure_if_needed(
    session: dict[str, Any],
    config: dict[str, Any],
    runtime: dict[str, Any],
    error: BaseException,
) -> None:
    if not is_provider_request_error(error):
        return
    metadata = session.setdefault("metadata", {})
    if metadata.get("lastProviderFailure") == error.record:
        return
    metadata["lastProviderFailure"] = copy.deepcopy(error.record)
    _safe_emit(runtime, "provider.failed", {"session_id": session["id"], **error.record})
    await persist_provider_failure(session, config, error.record)


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
    no_auth = ((config.get("providerProfile") or {}).get("auth") or {}).get("kind") == "none"
    if config.get("provider") == "openai":
        if not config.get("openaiToken") and not no_auth:
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
            resolved = await resolve_provider_config_secrets(config)

            async def send(resolved: dict[str, Any]) -> dict[str, Any]:
                return await send_openai_request(
                    token=resolved.get("openaiToken"),
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

            try:
                return await send(resolved)
            except ProviderRequestError as error:
                account = config.get("openaiToken")
                if not (_chatgpt_account(config) and error.record.get("status") == 401 and not error.record.get("hadAssistantOutput")):
                    raise
                # A ChatGPT account's token can be refused before its expiry:
                # one forced refresh (unless another caller already made one), one retry.
                from ..providers.chatgpt_account import current_access_token
                await current_access_token(dict(account), force=True, stale=resolved.get("openaiToken"))
                return await send(await resolve_provider_config_secrets(config))

        return {"send": send_openai}
    if config.get("provider") == "google":
        if not config.get("googleApiKey") and not no_auth:
            return {"send": lambda request: _missing_google_credential_response(config, request)}

        async def send_google(request: dict[str, Any]) -> dict[str, Any]:
            resolved = await resolve_provider_config_secrets(config)
            previous = metadata.get("googleCache") if isinstance(metadata.get("googleCache"), dict) else None
            response = await send_google_interactions_request(
                config=resolved,
                request=request,
                runtime=runtime,
                turnSession=provider_turn_session,
                cachePrefixStepCount=(previous or {}).get("inputCount"),
                **timeout_policy,
            )
            _observe_google_cache(session, runtime, previous, response)
            return response

        return {"send": send_google}
    if config.get("provider") != "anthropic":
        # A kind this function does not know must never be sent as Anthropic.
        raise ValueError(f"Unsupported provider kind: {config.get('provider')!r}")
    if not config.get("apiKey") and not config.get("authToken") and not no_auth:
        return {"send": lambda request: _missing_anthropic_credential_response(config, request)}
    async def send_anthropic(request: dict[str, Any]) -> dict[str, Any]:
        resolved = await resolve_provider_config_secrets(config)
        return await send_anthropic_messages_request(
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
    await persist_session(session, config)
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

# The answer a turn gets when its profile has no credential. Hosts that must
# fail instead of answering (the headless runner) recognise it by this prefix.
MISSING_CREDENTIAL_PREFIX = "No credential is configured for provider profile"


def _missing_openai_credential_response(config: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    env_var = (((config.get("providerProfile") or {}).get("auth") or {}).get("envVar")) or "the required bearer token env var"
    return {
        "content": [
            {
                "type": "text",
                "text": f"{MISSING_CREDENTIAL_PREFIX} '{config.get('providerProfileName')}'. Set {env_var} or provide a Codex auth file.",
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
                "text": f"{MISSING_CREDENTIAL_PREFIX} '{config.get('providerProfileName')}'. Set {env_var} to enable model-backed responses.",
            }
        ],
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "model": request["model"],
        "stopReason": "end_turn",
    }


def _observe_google_cache(session: dict[str, Any] | None, runtime: dict[str, Any], previous: dict[str, Any] | None, response: dict[str, Any]) -> None:
    """Say what Gemini's cache did on this call and keep the snapshot the next call is compared with."""
    snapshot = response.pop("googleCache", None)
    if not isinstance(snapshot, dict) or not isinstance(session, dict):
        return
    metadata = session.setdefault("metadata", {})
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    sent = sum(int(usage.get(key) or 0) for key in ("input_tokens", "cache_read_input_tokens"))
    snapshot = {**snapshot, "compactions": int(metadata.get("compactions") or 0), "at": now_iso(), "inputTokens": sent}
    reason = diagnose_google_cache(previous, snapshot, usage, model=response.get("model"))
    for key in ("prefixHash", "prefixStableHash"):
        snapshot.pop(key, None)
    metadata["googleCache"] = snapshot
    _safe_emit(runtime, "provider.prompt_cache", google_cache_event(session.get("id"), usage, reason))


def _missing_google_credential_response(config: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    env_var = (((config.get("providerProfile") or {}).get("auth") or {}).get("envVar")) or "the Gemini API key env var the profile names"
    return {
        "content": [
            {
                "type": "text",
                "text": f"{MISSING_CREDENTIAL_PREFIX} '{config.get('providerProfileName')}'. Set {env_var} to enable model-backed responses.",
            }
        ],
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "model": request["model"],
        "stopReason": "end_turn",
    }


def _chatgpt_account(config: dict[str, Any]) -> bool:
    auth = (config.get("providerProfile") or {}).get("auth") or {}
    token = config.get("openaiToken")
    return auth.get("kind") == "chatgpt" and isinstance(token, dict) and token.get("purpose") == "provider_oauth_token"
