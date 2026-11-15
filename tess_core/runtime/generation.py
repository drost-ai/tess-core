from __future__ import annotations

from .._utils import is_positive_int as _positive_integer

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimeSessionBinding(Mapping[str, Any]):
    sessionId: str
    generation: int
    configRevision: int
    runtimeInstanceId: str = "standalone"
    pathGraphId: str = "unbound"

    def __getitem__(self, key: str) -> Any:
        if key not in {"sessionId", "generation", "configRevision", "runtimeInstanceId", "pathGraphId"}:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(("sessionId", "generation", "configRevision", "runtimeInstanceId", "pathGraphId"))

    def __len__(self) -> int:
        return 5


@dataclass(frozen=True, slots=True)
class RetiredRuntimeGeneration(Mapping[str, Any]):
    binding: RuntimeSessionBinding
    status: str

    def __getitem__(self, key: str) -> Any:
        if key == "binding":
            return self.binding
        if key == "status":
            return self.status
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(("binding", "status"))

    def __len__(self) -> int:
        return 2


@dataclass(frozen=True, slots=True)
class RuntimeActivationContext(Mapping[str, Any]):
    binding: RuntimeSessionBinding
    session: Mapping[str, Any]
    config: Mapping[str, Any]
    tools: tuple[Any, ...]
    slashCommands: tuple[Any, ...]
    workspace: Any
    plugins: Mapping[str, Any]
    mcp: Any
    bridge: Any
    retiredGeneration: RetiredRuntimeGeneration

    def __getitem__(self, key: str) -> Any:
        if key not in {
            "binding",
            "session",
            "config",
            "tools",
            "slashCommands",
            "workspace",
            "plugins",
            "mcp",
            "bridge",
            "retiredGeneration",
        }:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(
            (
                "binding",
                "session",
                "config",
                "tools",
                "slashCommands",
                "workspace",
                "plugins",
                "mcp",
                "bridge",
                "retiredGeneration",
            )
        )

    def __len__(self) -> int:
        return 10


class RuntimeActivationSupersededError(RuntimeError):
    code = "runtime.activation_superseded"

    def __init__(self, sessionId: str):
        super().__init__(
            f"Runtime activation for session {sessionId} was superseded by a newer request."
        )
        self.sessionId = sessionId


def create_runtime_session_binding(
    sessionId: str,
    generation: int,
    configRevision: int,
    *,
    runtimeInstanceId: str = "standalone",
    pathGraphId: str = "unbound",
) -> RuntimeSessionBinding:
    """Bind a session to a runtime generation and configuration revision.

    Work that carries a superseded binding can then be recognised and discarded.
    """
    if not isinstance(sessionId, str) or not sessionId:
        raise ValueError("Runtime session binding requires a non-empty session id.")
    if not _positive_integer(generation) or not _positive_integer(configRevision):
        raise ValueError("Runtime generation and config revision must be positive integers.")
    if not isinstance(runtimeInstanceId, str) or not runtimeInstanceId:
        raise ValueError("Runtime instance id must be a non-empty string.")
    if not isinstance(pathGraphId, str) or not pathGraphId:
        raise ValueError("Runtime path graph id must be a non-empty string.")
    return RuntimeSessionBinding(
        sessionId=sessionId,
        generation=generation,
        configRevision=configRevision,
        runtimeInstanceId=runtimeInstanceId,
        pathGraphId=pathGraphId,
    )


def create_runtime_activation_context(args: Mapping[str, Any]) -> RuntimeActivationContext:
    retired = args["retiredGeneration"]
    retired_generation = (
        retired
        if isinstance(retired, RetiredRuntimeGeneration)
        else RetiredRuntimeGeneration(binding=retired["binding"], status=retired["status"])
    )
    if retired_generation.status not in {"drained", "close_failed"}:
        raise ValueError("Retired runtime generation has an invalid status.")
    return RuntimeActivationContext(
        binding=args["binding"],
        session=MappingProxyType(dict(args["session"])),
        config=MappingProxyType(dict(args["config"])),
        tools=tuple(args.get("tools") or ()),
        slashCommands=tuple(args.get("slashCommands") or ()),
        workspace=args.get("workspace"),
        plugins=_freeze_mapping(args.get("plugins") or {}),
        mcp=args.get("mcp"),
        bridge=args.get("bridge"),
        retiredGeneration=retired_generation,
    )


def same_runtime_binding(left: Mapping[str, Any] | None, right: Mapping[str, Any] | None) -> bool:
    if left is None or right is None:
        return False
    return all(
        left.get(field) == right.get(field)
        for field in (
            "sessionId",
            "generation",
            "configRevision",
            "runtimeInstanceId",
            "pathGraphId",
        )
    )


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    frozen: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, dict):
            frozen[key] = _freeze_mapping(item)
        elif isinstance(item, list):
            frozen[key] = tuple(item)
        else:
            frozen[key] = item
    return MappingProxyType(frozen)


