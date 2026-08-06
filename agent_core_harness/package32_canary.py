from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from typing import Any, Awaitable, Callable

from agent_core._utils import now_iso
from agent_core.providers.provider_prompt_cache import (
    attachPromptCacheDispatchContext,
    commitProviderPromptCacheObservation,
)
from agent_core.providers.prompt_cache_state import readPromptCacheState
from agent_core.providers.anthropic_cache import countAnthropicCacheControls
from agent_core.providers.anthropic.payloads import (
    buildAnthropicHeaders,
    buildAnthropicMessagesEnvelope,
)
from agent_core.providers.anthropic.request import sendAnthropicMessagesRequest
from agent_core.runtime.paths import resolveRuntimePaths
from agent_core.storage import ensureBaseLayout
from agent_core.context.token_accounting import measureRequestInputTokens
from agent_core.providers.usage_costs import estimateUsageCost

from .package32_canary_evidence import (
    Package32CanaryError,
    preparePrivateCanaryRuntime,
    requirePaidCanaryPreflight,
    reservePrivateEvidenceFile,
    safeFailureEvidence,
    writePrivateCanaryEvidence,
)


SendRequest = Callable[[dict[str, Any], dict[str, Any], bool], Awaitable[dict[str, Any]]]
MeasureTokens = Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]
SOURCE_REVIEWED_AT = "2026-07-17"
MODEL = "claude-opus-4-8"
JSON_REQUESTS = 5
STREAM_REQUESTS = 3
MAX_OUTPUT_TOKENS = 16


def runPackage32AnthropicCanary(options: dict[str, Any]) -> dict[str, Any]:
    preflight = requirePaidCanaryPreflight(options)
    preparePrivateCanaryRuntime(preflight["runtimeHome"])
    descriptor = reservePrivateEvidenceFile(preflight["evidenceFile"])
    started_at = now_iso()
    base_evidence = {
        "schemaVersion": 1,
        "package": 32,
        "status": "running",
        "startedAt": started_at,
        "routeClass": "official",
        "modelHash": hashlib.sha256(MODEL.encode("utf-8")).hexdigest(),
        "sourceReviewedAt": SOURCE_REVIEWED_AT,
        "requestCounts": {"json5m": JSON_REQUESTS, "stream1h": STREAM_REQUESTS},
        "maximumCostUsd": preflight["maxCostUsd"],
    }
    try:
        result = asyncio.run(
            _run_canary(
                preflight,
                options.get("_sendRequest") or _live_send,
                options.get("_measureTokens") or _live_measure,
            )
        )
        evidence = {
            **base_evidence,
            **result,
            "status": "passed",
            "completedAt": now_iso(),
        }
        writePrivateCanaryEvidence(descriptor, evidence)
        return evidence
    except BaseException as error:
        evidence = safeFailureEvidence(
            {**base_evidence, "completedAt": now_iso()},
            error,
        )
        writePrivateCanaryEvidence(descriptor, evidence)
        raise
    finally:
        os.close(descriptor)


async def _run_canary(
    preflight: dict[str, Any],
    send_request: SendRequest,
    measure_tokens: MeasureTokens,
) -> dict[str, Any]:
    runtime_home = preflight["runtimeHome"]
    paths = resolveRuntimePaths(candidate=runtime_home)
    workspace = os.path.join(runtime_home, "workspace")
    os.mkdir(workspace, 0o700)
    await ensureBaseLayout(workspace, paths)
    stable_prefix = _stable_prefix()
    config5m = _config(preflight, workspace, "5m", "explicit", True)
    config1h = _config(preflight, workspace, "1h", "auto", False)
    base_messages = _base_messages()
    json_shapes = (
        (base_messages, stable_prefix),
        (base_messages, stable_prefix),
        (_append_exchange(base_messages, 1), stable_prefix),
        (_append_exchange(_append_exchange(base_messages, 1), 2), stable_prefix),
        (_append_exchange(_append_exchange(base_messages, 1), 2), stable_prefix + " changed"),
    )
    stream_shapes = (
        base_messages,
        _append_exchange(base_messages, 1),
        _append_exchange(_append_exchange(base_messages, 1), 2),
    )
    templates = [
        _request(MODEL, system, messages) for messages, system in json_shapes
    ] + [_request(MODEL, stable_prefix, messages) for messages in stream_shapes]
    upper_costs = [
        _request_upper_cost(request, config5m if index < JSON_REQUESTS else config1h)
        for index, request in enumerate(templates)
    ]
    estimated_max = sum(upper_costs)
    if estimated_max > preflight["maxCostUsd"]:
        raise Package32CanaryError(
            "cost_ceiling_exceeded",
            "The conservative request estimate exceeds the authorized ceiling.",
        )

    measurement = await measure_tokens(config5m, templates[0])
    if measurement.get("source") != "anthropic_count" or int(measurement.get("inputTokens") or 0) <= 1_024:
        raise Package32CanaryError(
            "stable_prefix_too_short",
            "The exact Anthropic count did not prove a cacheable prefix over 1,024 tokens.",
        )

    actual_cost = 0.0
    records: list[dict[str, Any]] = []
    json_session = f"pc32-json-{uuid.uuid4().hex}"
    for index, (messages, system) in enumerate(json_shapes, start=1):
        _assert_remaining_cost(
            actual_cost,
            sum(upper_costs[index - 1 :]),
            preflight["maxCostUsd"],
        )
        if index == 4:
            config5m = _config(preflight, workspace, "5m", "explicit", True)
        record, cost = await _interaction(
            config=config5m,
            paths=paths,
            session_id=json_session,
            request=_request(MODEL, system, messages),
            index=index,
            scenario="C1-C3-C4",
            stream=False,
            send_request=send_request,
        )
        actual_cost += cost
        _assert_cost(actual_cost, preflight["maxCostUsd"])
        records.append(record)

    _assert_json_trace(records)
    await _assert_fork_is_fresh(config5m, paths, json_shapes[-1])

    stream_session = f"pc32-stream-{uuid.uuid4().hex}"
    for index, messages in enumerate(stream_shapes, start=1):
        absolute_index = JSON_REQUESTS + index - 1
        _assert_remaining_cost(
            actual_cost,
            sum(upper_costs[absolute_index:]),
            preflight["maxCostUsd"],
        )
        record, cost = await _interaction(
            config=config1h,
            paths=paths,
            session_id=stream_session,
            request=_request(MODEL, stable_prefix, messages),
            index=index,
            scenario="C2",
            stream=True,
            send_request=send_request,
        )
        actual_cost += cost
        _assert_cost(actual_cost, preflight["maxCostUsd"])
        records.append(record)

    _assert_stream_trace(records[JSON_REQUESTS:])
    return {
        "exactInputTokens": int(measurement["inputTokens"]),
        "estimatedMaximumCostUsd": round(estimated_max, 6),
        "actualCostUsd": round(actual_cost, 6),
        "assertions": {
            "stablePrefixOverMinimum": True,
            "jsonCreationThenRead": True,
            "movingBoundaries": True,
            "diagnosticsBetaSingle": True,
            "diagnosticsImmediateSuccessor": True,
            "resumeContinuity": True,
            "forkIsolation": True,
            "streamCreationThenRead": True,
            "automaticDisplacesTailB": True,
            "oneCommitPerResponse": True,
            "ttlDetailReconciled": True,
        },
        "requests": records,
    }


async def _interaction(
    *,
    config: dict[str, Any],
    paths: Any,
    session_id: str,
    request: dict[str, Any],
    index: int,
    scenario: str,
    stream: bool,
    send_request: SendRequest,
) -> tuple[dict[str, Any], float]:
    previous_read = await readPromptCacheState({"sessionId": session_id, "paths": paths})
    previous = previous_read.get("state")
    prepared = attachPromptCacheDispatchContext(
        {
            "request": request,
            "config": config,
            "runtime": {"sessionBinding": {"generation": 2 if index >= 4 else 1}},
            "session": {"id": session_id, "provider": "anthropic"},
            "paths": paths,
            "reactiveAttempt": 0,
            "preparation": "normal",
            "previousState": previous,
        }
    )
    envelope = buildAnthropicMessagesEnvelope(prepared, config, {"stream": stream})
    plan = envelope["cachePlan"]
    if countAnthropicCacheControls(envelope["payload"]) != plan["usedBreakpoints"]:
        raise Package32CanaryError("placement_mismatch", "Local cache placement count changed.")
    headers = buildAnthropicHeaders(
        config,
        {"stream": stream},
        envelope.get("headerFeatures"),
    )
    beta_tokens = [
        token.strip().lower()
        for token in headers.get("anthropic-beta", "").split(",")
        if token.strip()
    ]
    previous_id = (previous or {}).get("response", {}).get("id")
    sent_previous_id = (
        envelope.get("payload", {}).get("diagnostics", {}).get("previous_message_id")
    )
    diagnostics_requested = bool(envelope.get("diagnostics", {}).get("requested"))
    diagnostic_lineage_valid = (
        not diagnostics_requested or sent_previous_id == previous_id
    )
    response = await send_request(config, prepared, stream)
    await commitProviderPromptCacheObservation(response)
    current = (await readPromptCacheState({"sessionId": session_id, "paths": paths})).get("state")
    expected_revision = int((previous or {}).get("revision") or 0) + 1
    if not current or current.get("revision") != expected_revision:
        raise Package32CanaryError("commit_count_mismatch", "Terminal response did not commit exactly once.")
    usage = response.get("usage") or {}
    detail_complete = usage.get("cache_creation_ttl_detail_complete") is True
    cost = estimateUsageCost(usage, MODEL)
    record = {
        "scenario": scenario,
        "request": index,
        "mode": plan.get("mode"),
        "ttl": current.get("placement", {}).get("ttl"),
        "stream": stream,
        "placements": list(plan.get("placements") or []),
        "omittedReasons": [
            f"{item.get('placement')}:{item.get('reason')}"
            for item in plan.get("omitted") or []
        ],
        "usedBreakpoints": plan.get("usedBreakpoints"),
        "cacheCreationInputTokens": usage.get("cache_creation_input_tokens", 0),
        "cacheCreation5mInputTokens": usage.get("cache_creation_5m_input_tokens", 0),
        "cacheCreation1hInputTokens": usage.get("cache_creation_1h_input_tokens", 0),
        "cacheReadInputTokens": usage.get("cache_read_input_tokens", 0),
        "ttlDetailComplete": detail_complete,
        "providerReason": (response.get("promptCache") or {}).get("providerDiagnosis"),
        "localReason": current.get("diagnosis", {}).get("localReason"),
        "revision": current.get("revision"),
        "diagnosticBetaSingle": (
            sum(name.lower() == "anthropic-beta" for name in headers) == 1
            and beta_tokens.count("cache-diagnosis-2026-04-07") == 1
            if diagnostics_requested
            else None
        ),
        "diagnosticLineageValid": diagnostic_lineage_valid,
        "previousDiagnosticIdSent": bool(envelope.get("diagnostics", {}).get("previousMessageIdSent")),
        "cacheWriteUsd": round(float(cost["cacheWriteUsd"]), 6),
        "estimatedCostUsd": round(float(cost["totalUsd"]), 6),
    }
    return record, float(cost["totalUsd"])


def _assert_json_trace(records: list[dict[str, Any]]) -> None:
    if not records[0]["cacheCreationInputTokens"] > 0 or not any(
        record["cacheReadInputTokens"] > 0 for record in records[1:]
    ):
        raise Package32CanaryError("json_cache_not_observed", "C1 did not create and read cache input.")
    if any(
        record["usedBreakpoints"] > 4
        or "system" not in record["placements"]
        or "tools" not in record["placements"]
        for record in records
    ):
        raise Package32CanaryError("json_placement_invalid", "C1 placement/tool contract failed.")
    if any(
        record["cacheCreationInputTokens"] != record["cacheCreation5mInputTokens"]
        or record["cacheCreation1hInputTokens"] != 0
        or not record["ttlDetailComplete"]
        for record in records
    ):
        raise Package32CanaryError("json_ttl_detail_invalid", "C1 5m usage detail did not reconcile.")
    if not all("tail_b" in records[index]["placements"] for index in (2, 3)):
        raise Package32CanaryError("moving_boundary_invalid", "C1 moving A/B continuity failed.")
    if records[3]["cacheReadInputTokens"] <= 0 or records[3]["revision"] != 4:
        raise Package32CanaryError("resume_continuity_invalid", "C4 resume did not preserve read continuity.")
    expected_reasons = ("warmup_no_comparison", "no_divergence", None, None, "system_changed")
    for record, expected in zip(records, expected_reasons):
        if expected is not None and record["providerReason"] != expected:
            raise Package32CanaryError("diagnostics_shape_changed", "C3 provider diagnosis changed.")
    if records[0]["previousDiagnosticIdSent"] or not all(
        record["previousDiagnosticIdSent"] for record in records[1:]
    ):
        raise Package32CanaryError("diagnostics_lineage_invalid", "C3 immediate-successor lineage failed.")
    if not all(record["diagnosticLineageValid"] for record in records):
        raise Package32CanaryError("diagnostics_lineage_invalid", "C3 used a non-successor diagnostic id.")
    if not all(record["diagnosticBetaSingle"] for record in records):
        raise Package32CanaryError("diagnostics_beta_invalid", "C3 beta emission was not singular.")


def _assert_stream_trace(records: list[dict[str, Any]]) -> None:
    if not records[0]["cacheCreationInputTokens"] > 0 or not any(
        record["cacheReadInputTokens"] > 0 for record in records[1:]
    ):
        raise Package32CanaryError("stream_cache_not_observed", "C2 did not create and read cache input.")
    for index, record in enumerate(records):
        if (
            record["placements"] != ["automatic", "system", "tools", "tail_a"]
            or record["cacheCreationInputTokens"] != record["cacheCreation1hInputTokens"]
            or record["cacheCreation5mInputTokens"] != 0
            or not record["ttlDetailComplete"]
        ):
            raise Package32CanaryError("stream_trace_invalid", f"C2 request {index + 1} failed its contract.")
    expected_write_cost = records[0]["cacheCreation1hInputTokens"] * 10 / 1_000_000
    if abs(records[0]["cacheWriteUsd"] - expected_write_cost) > 0.0000005:
        raise Package32CanaryError("stream_pricing_invalid", "C2 1h cache pricing changed.")
    if not all("tail_b:budget" in record["omittedReasons"] for record in records[1:]):
        raise Package32CanaryError("stream_displacement_invalid", "C2 did not budget-displace B.")


async def _assert_fork_is_fresh(config: dict[str, Any], paths: Any, shape: tuple[list[dict[str, Any]], str]) -> None:
    fork_id = f"pc32-fork-{uuid.uuid4().hex}"
    state = await readPromptCacheState({"sessionId": fork_id, "paths": paths})
    prepared = attachPromptCacheDispatchContext(
        {
            "request": _request(MODEL, shape[1], shape[0]),
            "config": config,
            "runtime": {"sessionBinding": {"generation": 1}},
            "session": {"id": fork_id, "provider": "anthropic"},
            "paths": paths,
            "reactiveAttempt": 0,
            "preparation": "normal",
            "previousState": None,
        }
    )
    envelope = buildAnthropicMessagesEnvelope(prepared, config)
    if state.get("status") != "missing" or envelope["diagnostics"]["previousMessageIdSent"]:
        raise Package32CanaryError("fork_inherited_state", "C4 fork inherited cache state.")


def _config(preflight: dict[str, Any], workspace: str, ttl: str, mode: str, diagnostics: bool) -> dict[str, Any]:
    return {
        "cwd": workspace,
        "runtimeHome": preflight["runtimeHome"],
        "provider": "anthropic",
        "providerKind": "anthropic",
        "providerProfileName": preflight["providerProfileName"],
        "providerProfile": preflight["providerProfile"],
        "apiKey": preflight["apiKey"],
        "authToken": preflight["authToken"],
        "anthropicBaseUrl": preflight["anthropicBaseUrl"],
        "customHeaders": {},
        "model": MODEL,
        "promptCache": {
            "enabled": True,
            "mode": mode,
            "ttl": ttl,
            "explicit": True,
            "compatibility": "official",
            "providerDiagnostics": diagnostics,
        },
    }


def _request(model: str, system: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "model": model,
        "system": system,
        "messages": messages,
        "tools": [
            {
                "name": "canary_lookup",
                "description": "Return a deterministic canary value.",
                "input_schema": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                },
            }
        ],
        "maxOutputTokens": MAX_OUTPUT_TOKENS,
    }


def _stable_prefix() -> str:
    return " ".join(f"cache-prefix-{index:04d}" for index in range(768))


def _base_messages() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "A deterministic cache canary begins."},
        {"role": "assistant", "content": "Acknowledged."},
        {"role": "user", "content": "Reply with OK only."},
    ]


def _append_exchange(messages: list[dict[str, Any]], index: int) -> list[dict[str, Any]]:
    return [
        *messages,
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": f"Canary continuation {index}; reply OK."},
    ]


def _request_upper_cost(request: dict[str, Any], config: dict[str, Any]) -> float:
    payload = buildAnthropicMessagesEnvelope(request, config)["payload"]
    maximum_input_tokens = len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) + 512
    return maximum_input_tokens * 10 / 1_000_000 + MAX_OUTPUT_TOKENS * 25 / 1_000_000


def _assert_cost(actual: float, ceiling: float) -> None:
    if actual > ceiling:
        raise Package32CanaryError("cost_ceiling_exceeded", "Observed usage exceeded the authorized ceiling.")


def _assert_remaining_cost(actual: float, remaining_upper: float, ceiling: float) -> None:
    if actual + remaining_upper > ceiling:
        raise Package32CanaryError(
            "cost_ceiling_exceeded",
            "Remaining conservative request cost exceeds the authorized ceiling.",
        )


async def _live_send(config: dict[str, Any], request: dict[str, Any], stream: bool) -> dict[str, Any]:
    return await sendAnthropicMessagesRequest(
        {
            "config": config,
            "request": request,
            "runtime": {"streamProviderMessages": stream},
            "maxAttempts": 1,
        }
    )


async def _live_measure(config: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    return await measureRequestInputTokens(
        {"config": config, "request": request, "timeoutSeconds": 30}
    )


def main(argv: list[str] | None = None) -> int:
    from .package32_canary_cli import runPackage32CanaryCli

    return runPackage32CanaryCli(runPackage32AnthropicCanary, argv)


if __name__ == "__main__":
    raise SystemExit(main())
