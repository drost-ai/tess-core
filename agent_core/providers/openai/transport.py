from __future__ import annotations

from ...runtime.events.emit import safe_emit as _emit

import importlib
from types import SimpleNamespace

from ..._utils import get_value, maybe_await
from .._core import (
    race_provider_activity as _race_provider_activity,
    raise_if_provider_aborted as _raise_if_provider_aborted,
    signal_aborted as _signal_aborted,
)
from .params import isCodexBaseUrl
from .shared import OPENAI_ABORT_POLL_INTERVAL_MS
from typing import Any

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
    if factory:
        return factory(options)
    if _openai_transport_preference(args) == "sdk":
        return _create_openai_sdk_client(options)
    return _create_openai_wire_client(options)


def _openai_transport_preference(args: dict[str, Any]) -> str:
    config = args.get("config")
    preference = (
        str(config.get("openaiTransport") or "").strip().lower()
        if isinstance(config, dict)
        else ""
    )
    return "sdk" if preference == "sdk" else "wire"


def _create_openai_wire_client(options: dict[str, Any]) -> Any:
    from ._wire import OpenAiWireClient

    return OpenAiWireClient(
        api_key=options.get("apiKey"),
        base_url=options.get("baseURL"),
        default_headers=options.get("defaultHeaders"),
    )


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
    return await _race_provider_activity(
        awaitable,
        provider="openai",
        provider_label=provider_label,
        message_label="OpenAI",
        timeout_ms=timeout_ms,
        phase=phase,
        attempt=attempt,
        had_output=had_output,
        had_tool_use=had_tool_use,
        abort_signal=abort_signal,
        turn_session=turn_session,
        abort_poll_interval_ms=OPENAI_ABORT_POLL_INTERVAL_MS,
    )


class _ClientFactoryIdentity:
    """Identity token for a clientFactory in the turn-session client cache.

    Pins a strong reference so a recycled ``id()`` can never alias two
    factories, compares by object identity, and survives the cache's
    ``deepcopy`` untouched.
    """

    __slots__ = ("factory",)

    def __init__(self, factory: Any) -> None:
        self.factory = factory

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, _ClientFactoryIdentity) and self.factory is other.factory

    def __hash__(self) -> int:
        return id(self.factory)

    def __deepcopy__(self, _memo: Any) -> _ClientFactoryIdentity:
        return self


async def _acquire_openai_client(
    args: dict[str, Any], base_url: str | None
) -> tuple[Any, bool]:
    turn_session = args.get("turnSession") or args.get("turn_session")
    if turn_session is None or not hasattr(turn_session, "acquire_client"):
        return _create_openai_client(args, base_url), False
    factory = args.get("clientFactory") or args.get("client_factory")
    identity = (
        args.get("token"),
        base_url,
        tuple(sorted((args.get("headers") or {}).items())),
        _ClientFactoryIdentity(factory) if factory is not None else None,
        _openai_transport_preference(args),
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

def _raise_openai_cancelled(runtime: dict[str, Any], provider_label: str, phase: str, attempt: int, had_output: bool, had_tool_use: bool) -> None:
    _raise_if_provider_aborted(
        runtime.get("abortSignal"),
        provider="openai",
        provider_label=provider_label,
        message_label="OpenAI",
        phase=phase,
        attempt=attempt,
        had_output=had_output,
        had_tool_use=had_tool_use,
    )

