from __future__ import annotations

import asyncio
import os
from typing import Any

from ..config import getDefaultEffortForProvider, getDefaultModelForProvider, normalizeEffortForModel, normalizeModelName
from ..provider_errors import createProviderFailureRecord, createProviderRequestError
from ..provider_timeouts import resolveProviderTimeoutPolicy
from ..provider_token_budget import resolveCompactionPolicy
from ..providers.anthropic import sendAnthropicMessagesRequest
from ..providers.openai import DEFAULT_CODEX_BASE_URL, getOpenAiProviderLabel, sendOpenAiRequest
from ..storage import loadCodexAuthToken


CODEX_SPARK_COMPACTION_MODEL = "gpt-5.3-codex-spark"
DEFAULT_COMPACTION_SYNTHESIS_TIMEOUT_MS = 300_000
DEFAULT_COMPACTION_VERIFICATION_TIMEOUT_MS = 300_000

# Backward-compatible public names; runtime selection is now profile/session based.
FIXED_COMPACTION_PROVIDER = "openai"
FIXED_COMPACTION_MODEL = CODEX_SPARK_COMPACTION_MODEL
FIXED_COMPACTION_EFFORT = "high"
COMPACTION_SYNTHESIS_TIMEOUT_MS = DEFAULT_COMPACTION_SYNTHESIS_TIMEOUT_MS
COMPACTION_VERIFICATION_TIMEOUT_MS = DEFAULT_COMPACTION_VERIFICATION_TIMEOUT_MS


class CompactionModelClient:
    def __init__(self, complete):
        self.complete = complete

    def __getitem__(self, key: str):
        if key == "complete":
            return self.complete
        raise KeyError(key)


def createDefaultCompactionModelClient(options: dict[str, Any] | None = None) -> CompactionModelClient:
    options = options or {}

    async def complete(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        args = {**(args or {}), **kwargs}
        config = args["config"]
        model = args["model"]
        runtime = _build_compaction_runtime(args)
        profile_resolution = resolve_compaction_profile(config, model)
        timeout_policy = resolveProviderTimeoutPolicy({"profile": profile_resolution.get("profile")})
        token_policy = resolveCompactionTokenPolicy({"config": config, "model": model})
        max_output_tokens = int(args.get("maxOutputTokens") or token_policy["compactionOutputTokens"])
        if model["provider"] == "openai":
            transport = await _resolve_compaction_openai_transport(config, model)
            sender = options.get("openAiRequestSender") or sendOpenAiRequest
            client_factory = _resolve_openai_client_factory(args, runtime, options, config)
            response = await _run_compaction_request_with_timeout(
                provider="openai",
                providerLabel=getOpenAiProviderLabel(transport["mode"], transport.get("baseUrl")),
                attempt=1,
                timeoutMs=_resolve_compaction_timeout_ms(model.get("purpose")),
                run=lambda: sender(
                    {
                        "token": transport["token"],
                        "baseUrl": transport.get("baseUrl"),
                        "headers": transport.get("headers"),
                        "mode": transport["mode"],
                        "request": {"model": model["model"], "effort": model.get("effort"), "maxOutputTokens": max_output_tokens, "system": args["system"], "messages": [{"role": "user", "content": args["prompt"]}], "tools": []},
                        "requestOptions": (profile_resolution.get("profile") or {}).get("requestOptions") or {},
                        "runtime": runtime,
                        "clientFactory": client_factory,
                        **timeout_policy,
                    }
                ),
            )
            return {"text": _extract_text(response.get("content", [])), "usage": response.get("usage", {}), "model": model}
        anthropic_config = _resolve_compaction_anthropic_config(config, model)
        if not anthropic_config.get("authToken") and not anthropic_config.get("apiKey"):
            raise RuntimeError("Anthropic credential is required for Anthropic-backed compaction.")
        response = await _run_compaction_request_with_timeout(
            provider="anthropic",
            providerLabel="Anthropic",
            attempt=1,
            timeoutMs=_resolve_compaction_timeout_ms(model.get("purpose")),
            run=lambda: sendAnthropicMessagesRequest(config=anthropic_config, request={"model": model["model"], "effort": model.get("effort"), "maxOutputTokens": max_output_tokens, "system": args["system"], "messages": [{"role": "user", "content": args["prompt"]}], "tools": []}, runtime=runtime, **timeout_policy),
        )
        return {"text": _extract_text(response.get("content", [])), "usage": response.get("usage", {}), "model": model}

    return CompactionModelClient(complete)


def _build_compaction_runtime(args: dict[str, Any]) -> dict[str, Any]:
    source = args.get("runtime") or {}
    runtime: dict[str, Any] = {}
    if source.get("abortSignal") is not None:
        runtime["abortSignal"] = source["abortSignal"]
    if args.get("abortSignal") is not None:
        runtime["abortSignal"] = args["abortSignal"]
    return runtime


def _resolve_openai_client_factory(
    args: dict[str, Any],
    runtime: dict[str, Any],
    options: dict[str, Any],
    config: dict[str, Any],
) -> Any:
    source_runtime = args.get("runtime") or {}
    return (
        args.get("openaiClientFactory")
        or args.get("openAiClientFactory")
        or source_runtime.get("openaiClientFactory")
        or source_runtime.get("openAiClientFactory")
        or runtime.get("openaiClientFactory")
        or runtime.get("openAiClientFactory")
        or options.get("openaiClientFactory")
        or options.get("openAiClientFactory")
        or config.get("openaiClientFactory")
        or config.get("openAiClientFactory")
    )


def resolveCompactionExecutionPlan(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    config = args.get("config") or {}
    session = args.get("session") or {}
    session_provider = session.get("provider") or config.get("provider") or "openai"
    session_model = session.get("model") or config.get("model") or getDefaultModelForProvider(session_provider)
    session_effort = session.get("effort") or config.get("effort") or getDefaultEffortForProvider(session_provider)
    synthesis_profile = config.get("compactionProviderProfile")
    if synthesis_profile:
        synthesis = _build_selection_for_profile(synthesis_profile, session_effort, "synthesis")
    elif config.get("compactionModel") or config.get("compactionProvider"):
        synthesis = _build_selection(provider=config.get("compactionProvider") or session_provider, model=config.get("compactionModel") or session_model, effort=session_effort, purpose="synthesis", selectionSource="configured", providerProfileName=None)
    else:
        synthesis = _build_selection(provider=FIXED_COMPACTION_PROVIDER, model=FIXED_COMPACTION_MODEL, effort=FIXED_COMPACTION_EFFORT, purpose="synthesis", selectionSource="fixed", providerProfileName=None)

    verification_profile = config.get("compactionVerificationProviderProfile")
    if verification_profile:
        verification = _build_selection_for_profile(verification_profile, synthesis.get("effort") or session_effort, "verification")
    elif config.get("compactionVerificationModel") or config.get("compactionVerificationProvider"):
        verification = _build_selection(provider=config.get("compactionVerificationProvider") or synthesis["provider"], model=config.get("compactionVerificationModel") or synthesis["model"], effort=synthesis.get("effort") or session_effort, purpose="verification", selectionSource="configured", providerProfileName=None)
    else:
        verification = _build_selection(provider=synthesis["provider"], model=synthesis["model"], effort=synthesis.get("effort") or session_effort, purpose="verification", selectionSource=synthesis.get("selectionSource", "fixed"), providerProfileName=synthesis.get("providerProfileName"))
    return {"synthesis": synthesis, "verification": verification}


def resolveCompactionModelCandidates(args: dict[str, Any] | None = None, **kwargs: Any) -> list[dict[str, Any]]:
    args = {**(args or {}), **kwargs}
    plan = resolveCompactionExecutionPlan(args)
    return [plan["verification"] if args.get("purpose") == "verification" else plan["synthesis"]]


def resolveCompactionTokenPolicy(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    config = args.get("config") or {}
    model = args.get("model") or {}
    resolved = resolve_compaction_profile(config, model)
    profile = resolved.get("profile")
    if profile:
        policy_input = {
            "profileName": profile.get("name"),
            "kind": model.get("provider"),
            "apiMode": profile.get("apiMode"),
            "model": model.get("model"),
            "maxContextLength": profile.get("maxContextLength"),
            "maxOutputTokens": profile.get("maxOutputTokens"),
            "compactionThresholdTokens": profile.get("compactionThresholdTokens"),
            "compactionOutputTokens": profile.get("compactionOutputTokens"),
        }
    else:
        policy_input = {
            "profileName": config.get("providerProfileName"),
            "kind": model.get("provider"),
            "apiMode": config.get("openaiApiMode") if model.get("provider") == "openai" else None,
            "model": model.get("model"),
        }
    return {
        **resolveCompactionPolicy(policy_input),
        "profileName": (profile or {}).get("name") or config.get("providerProfileName"),
        "fallbackToSessionProfile": bool(resolved.get("fallbackToSessionProfile")),
    }


def resolve_compaction_profile(config: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    provider_profile_name = model.get("providerProfileName")
    profiles = config.get("providerProfiles") or {}
    if provider_profile_name:
        profile = profiles.get(provider_profile_name) or (_resolve_compaction_provider_binding(config, model) or {}).get("profile")
        if isinstance(profile, dict) and profile.get("kind") == model.get("provider"):
            return {"profile": profile, "fallbackToSessionProfile": False}
    if config.get("provider") == model.get("provider") and isinstance(config.get("providerProfile"), dict):
        return {"profile": config.get("providerProfile"), "fallbackToSessionProfile": False}
    for profile in profiles.values():
        if isinstance(profile, dict) and profile.get("kind") == model.get("provider") and str(profile.get("defaultModel") or "").strip() == model.get("model"):
            return {"profile": profile, "fallbackToSessionProfile": False}
    if _should_use_codex_compaction_transport(model.get("model")) and isinstance(profiles.get("codex"), dict) and profiles["codex"].get("kind") == "openai":
        return {"profile": profiles["codex"], "fallbackToSessionProfile": False}
    preferred_name = "openai" if model.get("provider") == "openai" else ("anthropic-console" if profiles.get("anthropic-console") else "anthropic-claude-code")
    preferred = profiles.get(preferred_name)
    if isinstance(preferred, dict) and preferred.get("kind") == model.get("provider"):
        return {"profile": preferred, "fallbackToSessionProfile": False}
    for profile in profiles.values():
        if isinstance(profile, dict) and profile.get("kind") == model.get("provider"):
            return {"profile": profile, "fallbackToSessionProfile": False}
    if isinstance(config.get("providerProfile"), dict):
        return {"profile": config.get("providerProfile"), "fallbackToSessionProfile": True}
    return {"profile": None, "fallbackToSessionProfile": True}


def _build_selection(provider: str, model: str, effort: str, purpose: str, selectionSource: str, providerProfileName: str | None = None) -> dict[str, Any]:
    normalized = model.strip()
    return {"provider": provider, "providerProfileName": providerProfileName, "model": normalized, "effort": normalizeEffortForModel(provider, normalized, effort), "selectionSource": selectionSource, "purpose": purpose}


def _build_selection_for_profile(binding: dict[str, Any], fallback_effort: str, purpose: str) -> dict[str, Any]:
    profile = binding.get("profile") or {}
    provider = profile.get("kind")
    model = normalizeModelName(profile.get("defaultModel") or getDefaultModelForProvider(provider), provider)
    return _build_selection(provider=provider, providerProfileName=binding.get("profileName") or profile.get("name"), model=model, effort=profile.get("reasoningEffort") or profile.get("effortLevel") or fallback_effort or getDefaultEffortForProvider(provider), purpose=purpose, selectionSource="configured")


async def _resolve_compaction_openai_transport(config: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    binding = _resolve_compaction_provider_binding(config, model)
    if binding and (binding.get("profile") or {}).get("kind") == "openai":
        if not binding.get("openaiToken"):
            raise RuntimeError(f"OpenAI credential is required for {binding.get('profileName')}-backed compaction.")
        profile = binding["profile"]
        mode = profile.get("apiMode") or "responses"
        return {
            "token": binding["openaiToken"],
            "baseUrl": profile.get("baseUrl") or (DEFAULT_CODEX_BASE_URL if mode == "codex" else None),
            "headers": binding.get("customHeaders"),
            "mode": mode,
        }
    if not model.get("providerProfileName") and _should_use_codex_compaction_transport(model.get("model")):
        token = await _resolve_codex_compaction_token(config)
        if not token:
            raise RuntimeError("Codex authentication is required for Codex-backed compaction.")
        return {"token": token, "baseUrl": DEFAULT_CODEX_BASE_URL, "headers": None, "mode": "codex"}
    if not config.get("openaiToken"):
        raise RuntimeError("OpenAI credential is required for OpenAI-backed compaction.")
    use_selected = config.get("provider") == "openai"
    return {
        "token": config["openaiToken"],
        "baseUrl": config.get("openaiBaseUrl") if use_selected else None,
        "headers": config.get("customHeaders") if use_selected else None,
        "mode": config.get("openaiApiMode") if use_selected else "responses",
    }


async def _resolve_codex_compaction_token(config: dict[str, Any]) -> str | None:
    if config.get("providerProfileName") == "codex" and config.get("openaiApiMode") == "codex" and config.get("openaiToken"):
        return config["openaiToken"]
    if not config.get("openaiCodexAuthPath"):
        return None
    return await loadCodexAuthToken(config["openaiCodexAuthPath"])


def _resolve_compaction_anthropic_config(config: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    binding = _resolve_compaction_provider_binding(config, model)
    if not binding or (binding.get("profile") or {}).get("kind") != "anthropic":
        return config
    profile = binding.get("profile") or {}
    return {
        **config,
        "apiKey": binding.get("apiKey"),
        "authToken": binding.get("authToken"),
        "anthropicBaseUrl": profile.get("baseUrl"),
        "customHeaders": binding.get("customHeaders") or {},
    }


def _resolve_compaction_provider_binding(config: dict[str, Any], model: dict[str, Any]) -> dict[str, Any] | None:
    if not model.get("providerProfileName"):
        return None
    for binding in (config.get("compactionProviderProfile"), config.get("compactionVerificationProviderProfile")):
        if (
            isinstance(binding, dict)
            and binding.get("profileName") == model.get("providerProfileName")
            and (binding.get("profile") or {}).get("kind") == model.get("provider")
        ):
            return binding
    return None


def _should_use_codex_compaction_transport(model: str | None) -> bool:
    return str(model or "").strip().lower() == CODEX_SPARK_COMPACTION_MODEL


def _extract_text(blocks: list[dict[str, Any]]) -> str:
    return "".join(block.get("text", "") for block in blocks if block.get("type") == "text")


async def _run_compaction_request_with_timeout(provider: str, providerLabel: str, attempt: int, timeoutMs: int, run) -> Any:
    try:
        return await asyncio.wait_for(run(), timeout=timeoutMs / 1000)
    except asyncio.TimeoutError:
        raise createProviderRequestError(
            createProviderFailureRecord(
                {
                    "provider": provider,
                    "providerLabel": providerLabel,
                    "phase": "response_complete",
                    "category": "timeout",
                    "retryable": True,
                    "attempt": attempt,
                    "hadAssistantOutput": False,
                    "hadToolUse": False,
                    "rawMessage": f"Compaction request timed out after {timeoutMs}ms.",
                }
            )
        )


def _resolve_compaction_timeout_ms(purpose: str | None) -> int:
    env_name = "TESS_COMPACTION_SYNTHESIS_TIMEOUT_MS" if purpose == "synthesis" else "TESS_COMPACTION_VERIFICATION_TIMEOUT_MS"
    try:
        parsed = int(float(os.environ.get(env_name, "")))
    except Exception:
        parsed = 0
    if parsed > 0:
        return parsed
    return DEFAULT_COMPACTION_SYNTHESIS_TIMEOUT_MS if purpose == "synthesis" else DEFAULT_COMPACTION_VERIFICATION_TIMEOUT_MS


resolveCompactionProfile = resolve_compaction_profile
