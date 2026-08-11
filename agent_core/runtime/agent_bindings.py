from __future__ import annotations

from .._utils import is_positive_int as _positive_integer

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from .generation import RuntimeSessionBinding, create_runtime_session_binding
from ..providers.partial_output import (
    assertPartialOutputSink as _assert_partial_output_sink,
    assertPartialOutputStateStore as _assert_partial_output_state_store,
)
from ..providers.partial_output_state import (
    assertPartialOutputRecoveryPolicy as _assert_partial_output_recovery_policy,
)


AGENT_RUNTIME_BINDINGS_SCHEMA_VERSION = 1
_GATEWAY_METHODS = (
    "startBackgroundCommand",
    "startMainSessionTask",
    "startSubagentTask",
    "startHealthCheck",
    "stopTask",
    "retryTask",
    "readTaskOutput",
    "sendTaskMessage",
    "readTaskEvents",
    "readTaskQuestions",
    "answerTaskQuestion",
    "skipTaskQuestion",
)


class RuntimeBindingsCompatibilityError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code



_CONTRACT_FIELDS = frozenset({
    "schemaVersion",
    "tools",
    "emitEvent",
    "daemon",
    "refreshMcpToolRegistry",
    "sessionBinding",
    "questionPolicy",
    "streamProviderMessages",
    "includePartialMessages",
    "abortSignal",
    "shouldStopAfterStableBoundary",
    "partialOutputSink",
    "partialOutputStateStore",
    "partialOutputAckTimeoutMs",
})

# Optional extension keys the execution runtime carries beyond the contract.
# Anything else in the bindings is a caller error: the versioned contract is
# only meaningful if unknown keys fail loudly instead of passing through.
_EXTENSION_FIELDS = frozenset({
    "openaiClientFactory",
    "partialOutputRecoveryPolicy",
    "repositoryIdentity",
})

# Public union for adapters that filter a wider component surface down to
# legal binding fields (the harness does this).
AGENT_RUNTIME_BINDING_FIELDS = frozenset(_CONTRACT_FIELDS | _EXTENSION_FIELDS)

def create_agent_runtime_bindings(
    args: Mapping[str, Any] | None = None, **kwargs: Any
) -> Mapping[str, Any]:
    values = {**dict(args or {}), **kwargs}
    binding = _coerce_binding(values.get("sessionBinding"))
    question_policy = values.get("questionPolicy")
    daemon = values.get("daemon")
    frozen: dict[str, Any] = {
        **values,
        "schemaVersion": AGENT_RUNTIME_BINDINGS_SCHEMA_VERSION,
        "tools": tuple(values.get("tools") or ()),
        "sessionBinding": binding,
        "questionPolicy": MappingProxyType(dict(question_policy or {})),
        "daemon": _freeze_daemon_binding(daemon),
    }
    unknown = sorted(set(frozen) - _CONTRACT_FIELDS - _EXTENSION_FIELDS)
    if unknown:
        raise _error(
            "runtime_bindings_unknown_field",
            f"Agent runtime bindings contain unknown fields: {', '.join(unknown)}.",
        )
    bindings = MappingProxyType(frozen)
    assert_agent_runtime_bindings(bindings)
    return bindings


def assert_agent_runtime_bindings(
    value: Any, expectedSessionId: str | None = None
) -> None:
    if value is None:
        raise _error("runtime_bindings_missing", "Versioned agent runtime bindings are required.")
    if not isinstance(value, Mapping):
        raise _error("runtime_bindings_invalid", "Agent runtime bindings must be an object.")
    version = value.get("schemaVersion")
    if not isinstance(version, int) or isinstance(version, bool):
        raise _error("runtime_bindings_invalid", "Agent runtime bindings contain an invalid schema version.")
    if version < AGENT_RUNTIME_BINDINGS_SCHEMA_VERSION:
        raise _error(
            "runtime_bindings_upgrade_required",
            f"Agent runtime bindings schema {version} is older than required schema {AGENT_RUNTIME_BINDINGS_SCHEMA_VERSION}.",
        )
    if version > AGENT_RUNTIME_BINDINGS_SCHEMA_VERSION:
        raise _error(
            "runtime_bindings_unsupported",
            f"Agent runtime bindings schema {version} is newer than supported schema {AGENT_RUNTIME_BINDINGS_SCHEMA_VERSION}.",
        )

    binding = value.get("sessionBinding")
    policy = value.get("questionPolicy")
    tools = value.get("tools")
    try:
        _assert_partial_output_sink(value.get("partialOutputSink"))
        _assert_partial_output_state_store(value.get("partialOutputStateStore"))
        _assert_partial_output_recovery_policy(value.get("partialOutputRecoveryPolicy"))
    except ValueError as exc:
        raise _error("runtime_bindings_invalid", str(exc)) from exc
    if (
        not isinstance(tools, (list, tuple))
        or not all(_is_tool_definition(tool) for tool in tools)
        or not callable(value.get("emitEvent"))
        or not callable(value.get("refreshMcpToolRegistry"))
        or not _valid_binding(binding, expectedSessionId)
        or not isinstance(policy, Mapping)
        or not isinstance(policy.get("interactive"), bool)
        or not callable(policy.get("askQuestion"))
        or not _valid_optional_callable(value.get("shouldStopAfterStableBoundary"))
        or not _valid_optional_bool(value.get("streamProviderMessages"))
        or not _valid_optional_bool(value.get("includePartialMessages"))
        or not _valid_abort_signal(value.get("abortSignal"))
        or not _is_daemon_binding(value.get("daemon"))
    ):
        raise _error(
            "runtime_bindings_invalid",
            "Agent runtime bindings are incomplete or malformed.",
        )


def materialize_agent_execution_runtime(bindings: Mapping[str, Any]) -> dict[str, Any]:
    assert_agent_runtime_bindings(bindings)
    policy = bindings["questionPolicy"]
    daemon = bindings["daemon"]
    runtime = {
        "interactive": policy["interactive"],
        "askQuestion": policy["askQuestion"],
        "emitEvent": bindings["emitEvent"],
        "tools": list(bindings["tools"]),
        "refreshMcpToolRegistry": bindings["refreshMcpToolRegistry"],
        "applyPendingMcpCatalogChanges": bindings["refreshMcpToolRegistry"],
        "sessionBinding": bindings["sessionBinding"],
    }
    if daemon["state"] == "connected":
        runtime["daemon"] = daemon["client"]
    else:
        runtime["daemonUnavailableReason"] = daemon["reason"]
    for name in (
        "streamProviderMessages",
        "includePartialMessages",
        "abortSignal",
        "shouldStopAfterStableBoundary",
        "partialOutputSink",
        "partialOutputStateStore",
        "partialOutputAckTimeoutMs",
    ):
        if name in bindings:
            runtime[name] = bindings[name]
    for name in sorted(_EXTENSION_FIELDS):
        if name in bindings:
            runtime[name] = bindings[name]
    return runtime


def is_task_supervisor_gateway(value: Any) -> bool:
    return isinstance(value, Mapping) and all(callable(value.get(name)) for name in _GATEWAY_METHODS)


def unavailable_daemon_binding(
    code: str = "runtime.daemon_unavailable",
    message: str = "The task supervisor daemon is unavailable.",
    guidance: str | None = None,
) -> Mapping[str, Any]:
    reason = {"code": code, "message": message}
    if guidance:
        reason["guidance"] = guidance
    return _freeze_daemon_binding({"state": "unavailable", "reason": reason})


def _coerce_binding(value: Any) -> RuntimeSessionBinding | Any:
    if isinstance(value, RuntimeSessionBinding):
        return value
    if isinstance(value, Mapping):
        try:
            return create_runtime_session_binding(
                value.get("sessionId"),
                value.get("generation"),
                value.get("configRevision"),
                runtimeInstanceId=value.get("runtimeInstanceId") or "standalone",
                pathGraphId=value.get("pathGraphId") or "unbound",
            )
        except (TypeError, ValueError):
            return MappingProxyType(dict(value))
    return value


def _freeze_daemon_binding(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return MappingProxyType({})
    if value.get("state") == "connected":
        return MappingProxyType({"state": "connected", "client": value.get("client")})
    reason = value.get("reason") if isinstance(value.get("reason"), Mapping) else {}
    return MappingProxyType(
        {"state": value.get("state"), "reason": MappingProxyType(dict(reason))}
    )


def _valid_binding(value: Any, expected_session_id: str | None) -> bool:
    return (
        isinstance(value, Mapping)
        and isinstance(value.get("sessionId"), str)
        and bool(value.get("sessionId"))
        and _positive_integer(value.get("generation"))
        and _positive_integer(value.get("configRevision"))
        and (expected_session_id is None or value.get("sessionId") == expected_session_id)
    )


def _is_daemon_binding(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    if value.get("state") == "connected":
        return is_task_supervisor_gateway(value.get("client"))
    if value.get("state") != "unavailable" or not isinstance(value.get("reason"), Mapping):
        return False
    reason = value["reason"]
    return (
        _bounded_string(reason.get("code"), 128)
        and _bounded_string(reason.get("message"), 2_048)
        and (reason.get("guidance") is None or _bounded_string(reason.get("guidance"), 2_048))
    )


def _is_tool_definition(value: Any) -> bool:
    if isinstance(value, Mapping):
        return (
            _bounded_string(value.get("name"), 256)
            and isinstance(value.get("description"), str)
            and isinstance(value.get("inputSchema"), Mapping)
            and callable(value.get("handler"))
        )
    return (
        _bounded_string(getattr(value, "name", None), 256)
        and isinstance(getattr(value, "description", None), str)
        and isinstance(getattr(value, "inputSchema", None), Mapping)
        and callable(getattr(value, "handler", None))
    )


def _valid_optional_callable(value: Any) -> bool:
    return value is None or callable(value)


def _valid_optional_bool(value: Any) -> bool:
    return value is None or isinstance(value, bool)


def _valid_abort_signal(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, Mapping):
        return isinstance(value.get("aborted"), bool)
    return isinstance(getattr(value, "aborted", None), bool)




def _bounded_string(value: Any, maximum: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _error(code: str, message: str) -> RuntimeBindingsCompatibilityError:
    return RuntimeBindingsCompatibilityError(code, message)


