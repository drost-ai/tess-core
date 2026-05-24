from __future__ import annotations

import asyncio
from typing import Any

from ..config import normalizeEffortForModel
from ..provider_errors import createProviderFailureRecord, createProviderRequestError
from ..providers.anthropic import sendAnthropicMessagesRequest
from ..providers.openai import DEFAULT_CODEX_BASE_URL, getOpenAiProviderLabel, sendOpenAiRequest
from ..storage import loadCodexAuthToken


FIXED_COMPACTION_PROVIDER = "openai"
FIXED_COMPACTION_MODEL = "gpt-5.3-codex-spark"
FIXED_COMPACTION_EFFORT = "high"
COMPACTION_SYNTHESIS_TIMEOUT_MS = 45_000
COMPACTION_VERIFICATION_TIMEOUT_MS = 30_000


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
        if model["provider"] == "openai":
            transport = await _resolve_compaction_openai_transport(config, model)
            sender = options.get("openAiRequestSender") or sendOpenAiRequest
            client_factory = _resolve_openai_client_factory(args, runtime, options, config)
            response = await _run_compaction_request_with_timeout(
                provider="openai",
                providerLabel=getOpenAiProviderLabel(transport["mode"], transport.get("baseUrl")),
                attempt=1,
                timeoutMs=COMPACTION_SYNTHESIS_TIMEOUT_MS if model.get("purpose") == "synthesis" else COMPACTION_VERIFICATION_TIMEOUT_MS,
                run=lambda: sender(
                    {
                        "token": transport["token"],
                        "baseUrl": transport.get("baseUrl"),
                        "headers": transport.get("headers"),
                        "mode": transport["mode"],
                        "request": {"model": model["model"], "effort": model.get("effort"), "system": args["system"], "messages": [{"role": "user", "content": args["prompt"]}], "tools": []},
                        "runtime": runtime,
                        "clientFactory": client_factory,
                    }
                ),
            )
            return {"text": _extract_text(response.get("content", [])), "usage": response.get("usage", {}), "model": model}
        if not config.get("authToken") and not config.get("apiKey"):
            raise RuntimeError("Anthropic credential is required for Anthropic-backed compaction.")
        response = await _run_compaction_request_with_timeout(
            provider="anthropic",
            providerLabel="Anthropic",
            attempt=1,
            timeoutMs=COMPACTION_SYNTHESIS_TIMEOUT_MS if model.get("purpose") == "synthesis" else COMPACTION_VERIFICATION_TIMEOUT_MS,
            run=lambda: sendAnthropicMessagesRequest(config=config, request={"model": model["model"], "effort": model.get("effort"), "system": args["system"], "messages": [{"role": "user", "content": args["prompt"]}], "tools": []}, runtime=runtime),
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
    synthesis = _build_selection(provider=config.get("compactionProvider") or FIXED_COMPACTION_PROVIDER, model=config.get("compactionModel") or FIXED_COMPACTION_MODEL, effort=FIXED_COMPACTION_EFFORT, purpose="synthesis", selectionSource="configured" if config.get("compactionModel") else "fixed")
    verification = _build_selection(provider=config.get("compactionVerificationProvider") or synthesis["provider"], model=config.get("compactionVerificationModel") or synthesis["model"], effort=synthesis.get("effort") or FIXED_COMPACTION_EFFORT, purpose="verification", selectionSource="configured" if config.get("compactionVerificationModel") else synthesis.get("selectionSource", "fixed"))
    return {"synthesis": synthesis, "verification": verification}


def resolveCompactionModelCandidates(args: dict[str, Any] | None = None, **kwargs: Any) -> list[dict[str, Any]]:
    args = {**(args or {}), **kwargs}
    plan = resolveCompactionExecutionPlan(args)
    return [plan["verification"] if args.get("purpose") == "verification" else plan["synthesis"]]


def _build_selection(provider: str, model: str, effort: str, purpose: str, selectionSource: str) -> dict[str, Any]:
    normalized = model.strip()
    return {"provider": provider, "model": normalized, "effort": normalizeEffortForModel(provider, normalized, effort), "selectionSource": selectionSource, "purpose": purpose}


async def _resolve_compaction_openai_transport(config: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    if model["model"].strip().lower() == FIXED_COMPACTION_MODEL:
        token = await _resolve_codex_compaction_token(config)
        if not token:
            raise RuntimeError("Codex authentication is required for Codex-backed compaction.")
        return {"token": token, "baseUrl": DEFAULT_CODEX_BASE_URL, "headers": None, "mode": "codex"}
    if not config.get("openaiToken"):
        raise RuntimeError("OpenAI credential is required for OpenAI-backed compaction.")
    return {"token": config["openaiToken"], "baseUrl": None, "headers": None, "mode": "responses"}


async def _resolve_codex_compaction_token(config: dict[str, Any]) -> str | None:
    if config.get("providerProfileName") == "codex" and config.get("openaiApiMode") == "codex" and config.get("openaiToken"):
        return config["openaiToken"]
    if not config.get("openaiCodexAuthPath"):
        return None
    return await loadCodexAuthToken(config["openaiCodexAuthPath"])


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
