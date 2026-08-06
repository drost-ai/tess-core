from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import agent_core as core
import agent_core.providers.anthropic as anthropic_module
import agent_core.providers.openai as openai_module
from agent_core.providers.turn_session import createProviderTurnSession

from ..model import ScenarioAssertionError
from .model import get_path, json_safe
from .openai import FakeOpenAIClientFactory
from .server import FakeProviderHttpServer


class AbortSignal:
    def __init__(self) -> None:
        self.aborted = False
        self.reason: BaseException | None = None
        self.event = asyncio.Event()

    def abort(self, reason: BaseException | None = None) -> None:
        self.aborted = True
        self.reason = reason
        self.event.set()

    async def wait(self) -> None:
        await self.event.wait()


class FakeProviderRuntime:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.results: list[dict[str, Any]] = []
        self.servers: list[FakeProviderHttpServer] = []

    async def run(self, data: dict[str, Any]) -> dict[str, Any]:
        provider = str(data.get("provider") or "")
        api = str(data.get("api") or "")
        signal = AbortSignal()
        runtime = self._runtime_options(data, signal)
        result = {"provider": provider, "api": api, "status": "passed"}
        abort_task = _schedule_abort(data, signal)
        try:
            response = await self._dispatch(provider, api, data, runtime, signal)
            result["response"] = json_safe(response)
        except Exception as exc:
            failure = _failure_record(exc)
            result.update({"status": "failed", "failure": failure})
            if not data.get("expectFailure"):
                self.results.append(result)
                raise ScenarioAssertionError(f"{provider}.{api} provider request failed: {failure.get('rawMessage') or exc}", category="provider")
        finally:
            if abort_task:
                abort_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await abort_task
        self.results.append(result)
        return result

    def close(self) -> None:
        for server in list(self.servers):
            server.close()
        self.servers.clear()

    def observations(self) -> dict[str, Any]:
        return {
            "provider_requests": [
                {
                    key: value
                    for key, value in request.items()
                    if key not in {"connectionId", "clientDisconnected"}
                }
                for request in self.requests
            ],
            "provider_events": self.events,
            "provider_results": self.results,
            "provider_state": {
                "requestCount": len(self.requests),
                "eventCount": len(self.events),
                "resultStatuses": [result["status"] for result in self.results],
            },
        }

    def _runtime_options(self, data: dict[str, Any], signal: AbortSignal) -> dict[str, Any]:
        options = dict(data.get("runtime") or {})
        options["abortSignal"] = signal
        options["emitEvent"] = lambda event, payload: self.events.append({"event": event, "data": json_safe(payload)})
        return options

    async def _dispatch(self, provider: str, api: str, data: dict[str, Any], runtime: dict[str, Any], signal: AbortSignal) -> Any:
        if provider == "openai" and api in {"responses", "chat_completions", "codex"}:
            return await self._run_openai(api, data, runtime, signal)
        if provider == "anthropic" and api == "messages":
            return await self._run_anthropic_messages(data, runtime)
        if provider == "anthropic" and api == "count_tokens":
            return await self._run_anthropic_count_tokens(data)
        raise ScenarioAssertionError(f"Unsupported fake provider request: {provider}.{api}", category="harness")

    async def _run_openai(self, api: str, data: dict[str, Any], runtime: dict[str, Any], signal: AbortSignal) -> dict[str, Any]:
        fake = FakeOpenAIClientFactory(api=api, script=data.get("script"), request_recorder=self.requests, abort_signal=signal)
        args = {
            "token": data.get("token", "test-token"),
            "headers": data.get("headers"),
            "baseUrl": data.get("baseUrl"),
            "request": _request(provider="openai", data=data),
            "runtime": runtime,
            "maxAttempts": data.get("maxAttempts", 1),
            "clientFactory": fake.client_factory,
        }
        _copy_timeout_options(data, args)
        original_delay = openai_module.providerRetryDelayMs
        openai_module.providerRetryDelayMs = lambda _attempt: 0
        try:
            if api == "chat_completions":
                return await core.sendOpenAiChatCompletionsRequest(args)
            args["mode"] = "codex" if api == "codex" else data.get("mode", "responses")
            return await core.sendOpenAiResponsesRequest(args)
        finally:
            openai_module.providerRetryDelayMs = original_delay

    async def _run_anthropic_messages(self, data: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
        server = FakeProviderHttpServer(provider="anthropic", script=data.get("script"), request_recorder=self.requests).start()
        self.servers.append(server)
        config = _anthropic_config(server, data)
        turn_session = data.get("_turnSession")
        owns_turn_session = turn_session is None
        if owns_turn_session:
            turn_session = createProviderTurnSession(
                logical_turn_id="harness-anthropic-turn",
                session={"id": "harness-anthropic-session", "provider": "anthropic"},
                config=config,
                runtime=runtime,
                model=str(data.get("model") or "claude-sonnet-4-6"),
            )
        args = {
            "config": config,
            "request": _request(provider="anthropic", data=data),
            "runtime": runtime,
            "maxAttempts": data.get("maxAttempts", 1),
            "turnSession": turn_session,
        }
        _copy_timeout_options(data, args)
        original_delay = anthropic_module.providerRetryDelayMs
        anthropic_module.providerRetryDelayMs = lambda _attempt: 0
        try:
            return await core.sendAnthropicMessagesRequest(args)
        finally:
            anthropic_module.providerRetryDelayMs = original_delay
            if owns_turn_session:
                await turn_session.close()

    async def _run_anthropic_count_tokens(self, data: dict[str, Any]) -> dict[str, Any]:
        server = FakeProviderHttpServer(provider="anthropic", script=data.get("script"), request_recorder=self.requests).start()
        self.servers.append(server)
        return await core.measureRequestInputTokens(
            {
                "config": _anthropic_config(server, data),
                "request": _request(provider="anthropic", data=data),
                "session": data.get("session") or {},
                "timeoutSeconds": data.get("timeoutSeconds", 2),
            }
        )


def assert_provider_response(state: dict[str, Any], data: dict[str, Any]) -> None:
    result = _select_result(state, data)
    _expect(result, "provider", data)
    _expect(result, "api", data)
    _expect(result, "status", data)
    failure = result.get("failure") or {}
    response = result.get("response") or {}
    for field in ("failureCategory", "failurePhase", "retryable", "hadAssistantOutput", "hadToolUse"):
        if field in data:
            key = _failure_key(field)
            if failure.get(key) != data[field]:
                raise ScenarioAssertionError(f"provider failure {key} expected {data[field]!r}, got {failure.get(key)!r}")
    for key in ("content", "usage", "model", "stopReason", "inputTokens", "source"):
        if key in data and response.get(key) != data[key]:
            raise ScenarioAssertionError(f"provider response {key} expected {data[key]!r}, got {response.get(key)!r}")
    if "textContains" in data:
        text = "".join(block.get("text", "") for block in response.get("content", []) if isinstance(block, dict))
        if data["textContains"] not in text:
            raise ScenarioAssertionError(f"provider response text did not contain {data['textContains']!r}")


def assert_provider_capture(state: dict[str, Any], data: dict[str, Any]) -> None:
    runtime = _runtime(state)
    requests = _filter_requests(runtime.requests, data)
    if "requestCount" in data and len(requests) != data["requestCount"]:
        raise ScenarioAssertionError(f"provider request count expected {data['requestCount']}, got {len(requests)}")
    if "eventsInclude" in data:
        event_names = [event["event"] for event in runtime.events]
        missing = [event for event in data["eventsInclude"] if event not in event_names]
        if missing:
            raise ScenarioAssertionError(f"provider events missing: {missing}")
    for check in data.get("requestPathEquals") or []:
        path = check["path"]
        expected = check.get("equals")
        actual = get_path(requests[-1] if requests else {}, path)
        if actual != expected:
            raise ScenarioAssertionError(f"provider request {path} expected {expected!r}, got {actual!r}")


def _request(provider: str, data: dict[str, Any]) -> dict[str, Any]:
    default_model = "claude-sonnet-4-6" if provider == "anthropic" else "gpt-5.4"
    return {
        "model": data.get("model", default_model),
        "effort": data.get("effort"),
        "system": data.get("system", "Harness provider system."),
        "messages": data.get("messages") or [{"role": "user", "content": "Hello from the harness."}],
        "tools": data.get("tools") or [],
        **(data.get("request") or {}),
    }


def _anthropic_config(server: FakeProviderHttpServer, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": "anthropic",
        "anthropicBaseUrl": server.base_url,
        "apiKey": data.get("apiKey", "test-anthropic-key"),
        "customHeaders": data.get("headers") or {},
        "providerCapabilities": data.get("providerCapabilities") or {},
    }


def _copy_timeout_options(source: dict[str, Any], target: dict[str, Any]) -> None:
    for key in ("requestStartTimeoutMs", "streamIdleTimeoutMs", "jsonResponseTimeoutMs"):
        if key in source:
            target[key] = source[key]


def _schedule_abort(data: dict[str, Any], signal: AbortSignal) -> asyncio.Task[None] | None:
    if "abortAfterMs" not in data:
        return None

    async def abort_later() -> None:
        await asyncio.sleep(max(0, int(data["abortAfterMs"])) / 1000)
        signal.abort()

    return asyncio.create_task(abort_later())


def _failure_record(exc: Exception) -> dict[str, Any]:
    record = getattr(exc, "record", None)
    if isinstance(record, dict):
        return json_safe(record)
    return {"category": "runtime", "rawMessage": str(exc), "retryable": False}


def _select_result(state: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    results = list(state.get("provider_results") or [])
    if not results:
        raise ScenarioAssertionError("No provider result is available")
    index = int(data.get("index", len(results) - 1))
    if index < 0:
        index = len(results) + index
    if index < 0 or index >= len(results):
        raise ScenarioAssertionError(f"Provider result index out of range: {index}")
    return results[index]


def _runtime(state: dict[str, Any]) -> FakeProviderRuntime:
    runtime = state.get("provider_runtime")
    if not isinstance(runtime, FakeProviderRuntime):
        raise ScenarioAssertionError("No fake provider runtime is available")
    return runtime


def _filter_requests(requests: list[dict[str, Any]], data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        request
        for request in requests
        if (not data.get("provider") or request.get("provider") == data["provider"])
        and (not data.get("api") or request.get("api") == data["api"])
    ]


def _expect(result: dict[str, Any], key: str, data: dict[str, Any]) -> None:
    if key in data and result.get(key) != data[key]:
        raise ScenarioAssertionError(f"provider result {key} expected {data[key]!r}, got {result.get(key)!r}")


def _failure_key(field: str) -> str:
    return {
        "failureCategory": "category",
        "failurePhase": "phase",
        "hadAssistantOutput": "hadAssistantOutput",
        "hadToolUse": "hadToolUse",
        "retryable": "retryable",
    }[field]
