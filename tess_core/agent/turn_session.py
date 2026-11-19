from __future__ import annotations

import copy
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ..runtime.agent_bindings import (
    assert_agent_runtime_bindings,
    materialize_agent_execution_runtime,
)
from ..config import (
    ensure_provider_for_model_selection,
    resolve_fallback_provider_profile_name,
)
from ..providers.turn_session import create_provider_turn_session
from ..runtime.paths import get_active_runtime_paths, resolve_runtime_paths, run_with_runtime_paths
from ..runtime.transient_input import (
    create_transient_execution_session,
    sync_transient_execution_session,
)


async def run_with_transient_execution_session(
    args: dict[str, Any],
    run: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    canonical_session = args["session"]
    assert_agent_runtime_bindings(args.get("runtime"), canonical_session.get("id"))
    config = args["config"]
    paths = (
        resolve_runtime_paths(candidate=config.get("runtimeHome"))
        if config.get("runtimeHome")
        else get_active_runtime_paths()
    )
    execution_session = create_transient_execution_session(
        canonical_session,
        args.get("transientInputOverlay"),
    )
    values = {**args, "session": execution_session}
    try:
        return await run_with_runtime_paths(paths, lambda: run(values))
    finally:
        if execution_session is not canonical_session:
            sync_transient_execution_session(canonical_session, execution_session)


def resolve_effective_provider_turn_binding(
    args: dict[str, Any],
) -> dict[str, Any]:
    session = args["session"]
    config = args["config"]
    active_model = args.get("modelOverride") or session["model"]
    registry = config.get("providerRegistry")
    if registry is not None:
        profile_name = session.get("providerProfileName") or config.get("providerProfileName")
        profile = registry.resolve_profile(profile_name, model=active_model)
        effective_provider = profile["kind"]
        profiles = {profile_name: profile}
        if profile_name != config.get("providerProfileName"):
            from ..config import ProviderRegistryError
            raise ProviderRegistryError("Provider profile changes require explicit session configuration")
    else:
        effective_provider = ensure_provider_for_model_selection(
            session.get("provider") or config["provider"],
            active_model,
            config["provider"],
        )
        profiles = (
            config.get("providerProfiles")
            if isinstance(config.get("providerProfiles"), dict)
            else {}
        )
        profile_name = resolve_fallback_provider_profile_name(
            effective_provider,
            profiles,
            session.get("providerProfileName") or config.get("providerProfileName"),
        )
    current_profile_matches = (
        config.get("provider") == effective_provider
        and config.get("providerProfileName") == profile_name
        and isinstance(config.get("providerProfile"), dict)
    )
    profile = (
        config.get("providerProfile")
        if current_profile_matches
        else profiles.get(profile_name)
    )
    if not isinstance(profile, dict):
        profile = {}
    current_capabilities = (
        config.get("providerCapabilities")
        if current_profile_matches
        and isinstance(config.get("providerCapabilities"), dict)
        else None
    )
    session["provider"] = effective_provider
    session["providerProfileName"] = profile_name
    effective_config = {
        **config,
        "provider": effective_provider,
        "providerKind": effective_provider,
        "providerProfileName": profile_name,
        "providerProfile": profile,
        "providerCapabilities": copy.deepcopy(
            current_capabilities or profile.get("capabilities") or {}
        ),
        "customHeaders": copy.deepcopy(
            profile.get("headers")
            or (config.get("customHeaders") if current_profile_matches else {})
            or {}
        ),
    }
    if effective_provider == "openai":
        effective_config["openaiApiMode"] = profile.get("apiMode") or (
            config.get("openaiApiMode") if current_profile_matches else None
        )
        effective_config["openaiBaseUrl"] = profile.get("baseUrl") or (
            config.get("openaiBaseUrl") if current_profile_matches else None
        )
        effective_config["anthropicBaseUrl"] = None
        effective_config["googleBaseUrl"] = None
    elif effective_provider == "google":
        effective_config["googleBaseUrl"] = profile.get("baseUrl") or (
            config.get("googleBaseUrl") if current_profile_matches else None
        )
        effective_config["anthropicBaseUrl"] = None
        effective_config["openaiBaseUrl"] = None
    else:
        effective_config["anthropicBaseUrl"] = profile.get("baseUrl") or (
            config.get("anthropicBaseUrl") if current_profile_matches else None
        )
        effective_config["openaiBaseUrl"] = None
        effective_config["googleBaseUrl"] = None
    return {**args, "config": effective_config}


async def run_in_provider_turn_session(
    args: dict[str, Any],
    run: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    *,
    is_cancelled: Callable[[dict[str, Any]], bool],
) -> dict[str, Any]:
    session = args["session"]
    config = args["config"]
    runtime_bindings = args.get("runtime")
    runtime = materialize_agent_execution_runtime(runtime_bindings)
    args = {**args, "materializedRuntime": runtime}
    logical_turn_id = _resolve_logical_turn_id(args.get("logicalTurnId"))
    provider_turn_session = create_provider_turn_session(
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


def _resolve_logical_turn_id(value: Any) -> str:
    if value is None:
        return f"turn-{uuid.uuid4()}"
    if not isinstance(value, str):
        raise TypeError("logicalTurnId must be a string when provided.")
    logical_turn_id = value.strip()
    if (
        not logical_turn_id
        or len(logical_turn_id) > 128
        or any(ord(character) < 0x20 for character in logical_turn_id)
    ):
        raise ValueError(
            "logicalTurnId must contain 1-128 visible characters."
        )
    return logical_turn_id


__all__ = [
    "resolve_effective_provider_turn_binding",
    "run_in_provider_turn_session",
    "run_with_transient_execution_session",
]


async def run_scoped_agent_turn(values, callback, is_cancelled):
    from ..runtime.execution_budget import run_with_execution_budget
    async def run_in_scope(scoped_args):
        return await run_in_provider_turn_session(
            resolve_effective_provider_turn_binding(scoped_args), callback,
            is_cancelled=is_cancelled,
        )
    return await run_with_execution_budget(values, lambda: run_with_transient_execution_session(values, run_in_scope))
