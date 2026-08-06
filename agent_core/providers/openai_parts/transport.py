from __future__ import annotations

import importlib
from types import SimpleNamespace

from .shared import *

def _infer_openai_responses_mode(base_url: str | None) -> str:
    return "codex" if isCodexBaseUrl(base_url) else "responses"

def _create_openai_client(args: dict[str, Any], base_url: str | None) -> Any:
    _assert_openai_transport_secrets(args)
    factory = args.get("clientFactory") or args.get("client_factory")
    options = {"apiKey": args.get("token"), "dangerouslyAllowBrowser": False, "fetch": _build_openai_fetch(args.get("runtime") or {})}
    if args.get("headers"):
        options["defaultHeaders"] = args["headers"]
    if base_url:
        options["baseURL"] = base_url
    if not factory:
        return _create_openai_sdk_client(options)
    return factory(options)


def _assert_openai_transport_secrets(args: dict[str, Any]) -> None:
    from ...runtime.secrets import RuntimeSecretError

    token = args.get("token")
    headers = args.get("headers") or {}
    if not isinstance(token, str) or any(not isinstance(value, str) for value in headers.values()):
        raise RuntimeSecretError(
            "secret_store_invalid",
            "provider credential reached a transport before resolution",
        )


class _OpenAiResponsesSdkAdapter:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def create(self, params: dict[str, Any]) -> Any:
        return await maybe_await(self._client.responses.create(**params))


class _OpenAiChatCompletionsSdkAdapter:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def create(self, params: dict[str, Any]) -> Any:
        return await maybe_await(self._client.chat.completions.create(**params))


def _create_openai_sdk_client(options: dict[str, Any]) -> Any:
    try:
        openai_module = importlib.import_module("openai")
    except Exception as exc:
        raise RuntimeError(
            "A clientFactory is required unless the optional OpenAI SDK transport is available."
        ) from exc
    async_openai = getattr(openai_module, "AsyncOpenAI", None)
    if async_openai is None:
        raise RuntimeError("The optional OpenAI SDK transport is unavailable because AsyncOpenAI is missing.")
    kwargs: dict[str, Any] = {"api_key": options.get("apiKey")}
    if options.get("baseURL"):
        kwargs["base_url"] = options["baseURL"]
    if options.get("defaultHeaders"):
        kwargs["default_headers"] = options["defaultHeaders"]
    client = async_openai(**kwargs)
    return SimpleNamespace(
        responses=_OpenAiResponsesSdkAdapter(client),
        chat=SimpleNamespace(completions=_OpenAiChatCompletionsSdkAdapter(client)),
        capabilities={},
        close=getattr(client, "close", None),
    )

def _build_openai_fetch(runtime: dict[str, Any]):
    async def fetch(_input: Any, init: dict[str, Any] | None = None) -> Any:
        signal = get_value(init or {}, "signal") or runtime.get("abortSignal")
        if _signal_aborted(signal):
            raise RuntimeError("The operation was aborted.")
        raise RuntimeError("OpenAI fetch transport is only available through the configured clientFactory.")

    return fetch

async def _race_openai_activity(
    awaitable: Any,
    *,
    timeout_ms: int,
    provider_label: str,
    phase: str,
    attempt: int,
    had_output: bool,
    had_tool_use: bool,
    abort_signal: Any = None,
    turn_session: Any = None,
) -> Any:
    _raise_openai_cancelled({"abortSignal": abort_signal}, provider_label, phase, attempt, had_output, had_tool_use)
    task = asyncio.ensure_future(awaitable)
    if turn_session is not None:
        turn_session.bind_activity_task(task)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (max(1, timeout_ms) / 1000)
    poll_seconds = OPENAI_ABORT_POLL_INTERVAL_MS / 1000
    while True:
        if task.done():
            return task.result()
        if _signal_aborted(abort_signal):
            await _cancel_openai_activity_task(task)
            _raise_openai_cancelled({"abortSignal": abort_signal}, provider_label, phase, attempt, had_output, had_tool_use)
        remaining = deadline - loop.time()
        if remaining <= 0:
            await _cancel_openai_activity_task(task)
            _raise_openai_cancelled({"abortSignal": abort_signal}, provider_label, phase, attempt, had_output, had_tool_use)
            raise _create_openai_timeout(provider_label, phase, attempt, had_output, had_tool_use, timeout_ms)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=min(poll_seconds, remaining))
        except asyncio.TimeoutError:
            if task.done():
                return task.result()
            continue
        finally:
            if task.done() and turn_session is not None:
                turn_session.clear_activity_task(task)


async def _acquire_openai_client(
    args: dict[str, Any], base_url: str | None
) -> tuple[Any, bool]:
    turn_session = args.get("turnSession") or args.get("turn_session")
    if turn_session is None or not hasattr(turn_session, "acquire_client"):
        return _create_openai_client(args, base_url), False
    identity = (
        args.get("token"),
        base_url,
        tuple(sorted((args.get("headers") or {}).items())),
        id(args.get("clientFactory") or args.get("client_factory")),
    )
    return await turn_session.acquire_client(
        identity=identity,
        create=lambda: _create_openai_client(args, base_url),
    )


def _plain_openai_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain_openai_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_openai_value(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _plain_openai_value(model_dump(exclude_none=True))
        except TypeError:
            return _plain_openai_value(model_dump())
    if hasattr(value, "__dict__"):
        return {
            str(key): _plain_openai_value(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return value

def _create_openai_timeout(provider_label: str, phase: str, attempt: int, had_output: bool, had_tool_use: bool, timeout_ms: int) -> Any:
    code = (
        "provider_request_start_timeout"
        if phase == "request_start"
        else "provider_stream_idle_timeout"
    )
    return createProviderRequestError(
        createProviderFailureRecord(
            provider="openai",
            providerLabel=provider_label,
            phase=phase,
            category="timeout",
            retryable=True,
            attempt=attempt,
            hadAssistantOutput=had_output,
            hadToolUse=had_tool_use,
            code=code,
            rawMessage=f"OpenAI request timed out after {timeout_ms}ms while waiting for provider activity.",
        )
    )

async def _cancel_openai_activity_task(task: asyncio.Future[Any]) -> None:
    if task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task

def _raise_openai_cancelled(runtime: dict[str, Any], provider_label: str, phase: str, attempt: int, had_output: bool, had_tool_use: bool) -> None:
    if not _signal_aborted(runtime.get("abortSignal")):
        return
    raise createProviderRequestError(
        createProviderFailureRecord(
            provider="openai",
            providerLabel=provider_label,
            phase=phase,
            category="cancelled",
            retryable=False,
            attempt=attempt,
            hadAssistantOutput=had_output,
            hadToolUse=had_tool_use,
            rawMessage="OpenAI request was cancelled.",
        )
    )

def _signal_aborted(signal: Any) -> bool:
    if not signal:
        return False
    if isinstance(signal, dict):
        return bool(signal.get("aborted"))
    return bool(getattr(signal, "aborted", False))

def _emit(runtime: dict[str, Any], event: str, data: dict[str, Any]) -> None:
    fn = runtime.get("emitEvent")
    if fn:
        fn(event, data)
