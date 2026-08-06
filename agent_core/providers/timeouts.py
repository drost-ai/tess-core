from __future__ import annotations

import time
from typing import Any, Mapping

from .errors import createProviderFailureRecord, createProviderRequestError
from .watchdog import ProviderRequestWatchdog, createProviderRequestWatchdog


DEFAULT_TOKEN_COUNT_TIMEOUT_MS = 15_000
DEFAULT_TOTAL_REQUEST_TIMEOUT_MS = 600_000


class TurnDeadlineContext:
    def __init__(
        self,
        *,
        watchdog: ProviderRequestWatchdog,
        total_deadline_at: float,
        token_count_timeout_ms: int,
        request_start_timeout_ms: int,
        stream_idle_timeout_ms: int,
    ) -> None:
        self._watchdog = watchdog
        self.signal = watchdog.signal
        self.totalDeadlineAt = total_deadline_at
        self.tokenCountTimeoutMs = token_count_timeout_ms
        self.requestStartTimeoutMs = request_start_timeout_ms
        self.streamIdleTimeoutMs = stream_idle_timeout_ms

    async def race(self, awaitable: Any) -> Any:
        return await self._watchdog.race(awaitable)

    def remainingMs(self) -> int:
        return max(0, int((self.totalDeadlineAt - time.monotonic()) * 1000))

    def dispose(self) -> None:
        self._watchdog.dispose()

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


def resolve_provider_timeout_policy(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, int]:
    args = {**(args or {}), **kwargs}
    profile = args.get("profile") or (args.get("config") or {}).get("providerProfile") or {}
    per_call = args.get("perCallOptions") or {}
    runtime_contract = args.get("runtimeContract") or (args.get("config") or {}).get("subagentRuntimeContract")
    contract_timeout = _positive_int(runtime_contract.get("timeoutMs")) if isinstance(runtime_contract, Mapping) else None
    policy: dict[str, int] = {}
    request_start = (
        _positive_int(per_call.get("requestStartTimeoutMs"))
        or _positive_int(profile.get("requestStartTimeoutMs"))
        or contract_timeout
    )
    stream_idle = (
        _positive_int(per_call.get("streamIdleTimeoutMs"))
        or _positive_int(profile.get("streamIdleTimeoutMs"))
        or contract_timeout
    )
    json_response = _positive_int(per_call.get("jsonResponseTimeoutMs")) or contract_timeout
    if request_start:
        policy["requestStartTimeoutMs"] = request_start
    if stream_idle:
        policy["streamIdleTimeoutMs"] = stream_idle
    if json_response:
        policy["jsonResponseTimeoutMs"] = json_response
    return policy


def create_turn_deadline_context(
    args: dict[str, Any] | None = None,
    **kwargs: Any,
) -> TurnDeadlineContext:
    args = {**(args or {}), **kwargs}
    config = args["config"]
    profile = config.get("providerProfile") or {}
    per_call = args.get("perCallOptions") or {}
    runtime_contract = args.get("runtimeContract") or config.get("subagentRuntimeContract")
    contract_timeout = (
        _positive_int(runtime_contract.get("timeoutMs"))
        if isinstance(runtime_contract, Mapping)
        else None
    )
    transport = resolve_provider_timeout_policy(
        config=config,
        perCallOptions=per_call,
        runtimeContract=runtime_contract,
    )
    token_count_timeout_ms = (
        _positive_int(per_call.get("tokenCountTimeoutMs"))
        or _positive_int(profile.get("tokenCountTimeoutMs"))
        or DEFAULT_TOKEN_COUNT_TIMEOUT_MS
    )
    total_request_timeout_ms = (
        _positive_int(per_call.get("totalRequestTimeoutMs"))
        or _positive_int(profile.get("totalRequestTimeoutMs"))
        or contract_timeout
        or DEFAULT_TOTAL_REQUEST_TIMEOUT_MS
    )
    request_start_timeout_ms = (
        transport.get("requestStartTimeoutMs") or 120_000
    )
    stream_idle_timeout_ms = transport.get("streamIdleTimeoutMs") or 60_000
    total_deadline_at = time.monotonic() + (total_request_timeout_ms / 1000)
    watchdog = createProviderRequestWatchdog(
        parentSignal=args.get("parentSignal"),
        initialTimeoutMs=total_request_timeout_ms,
        buildTimeoutError=lambda: createProviderRequestError(
            createProviderFailureRecord(
                provider=config.get("provider"),
                providerLabel=config.get("providerProfileName") or config.get("provider"),
                phase="request_start",
                category="timeout",
                retryable=True,
                attempt=1,
                hadAssistantOutput=False,
                hadToolUse=False,
                rawMessage=(
                    "Provider request cycle exceeded its "
                    f"{total_request_timeout_ms}ms total deadline."
                ),
            )
        ),
    )
    return TurnDeadlineContext(
        watchdog=watchdog,
        total_deadline_at=total_deadline_at,
        token_count_timeout_ms=token_count_timeout_ms,
        request_start_timeout_ms=request_start_timeout_ms,
        stream_idle_timeout_ms=stream_idle_timeout_ms,
    )


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(float(str(value).strip())) if str(value).strip() else 0
    except Exception:
        return None
    return parsed if parsed > 0 else None


resolveProviderTimeoutPolicy = resolve_provider_timeout_policy
createTurnDeadlineContext = create_turn_deadline_context
