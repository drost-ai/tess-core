from __future__ import annotations

import asyncio, threading, uuid
from typing import Any, Callable, Mapping

from .._utils import maybe_await
from ..tasks.collaboration import createCollaborationRuntime
from ..daemon import ensureTaskSupervisorDaemon, stopTaskSupervisorDaemon
from ._daemon_watch import start_daemon_watchdog as _start_daemon_watchdog
from .exception_groups import aggregateRuntimeFailures as _aggregate_runtime_failures
from .lifecycle_barrier import createLifecycleBarrier
from ..tasks.monitors import createMonitorRuntime
from .events import createRuntimeEvent
from .generation import RuntimeActivationSupersededError, createRuntimeActivationContext, createRuntimeSessionBinding, sameRuntimeBinding
from .paths import RuntimePaths, getActiveRuntimePaths, runWithRuntimePaths
from .session_components import prepareRuntimeSessionComponents
from .support import StagedRuntimeSource as _StagedRuntimeSource, acquireCommit as _acquire_commit, defaultRuntimeComponentFactories as _default_factories
from .support import callMethod as _call_method, closeMany as _close_many, daemonSnapshotPayload as _daemon_snapshot_payload, getMethod as _get_method, hasMethod as _has_method
from .support import installComponentSurface as _install_component_surface, maybeCall as _maybe_call, runtimePathGraphId as _path_graph_id, taskSnapshotFromState as _task_snapshot_from_state
from .state import createInitialRuntimeState, createRuntimeStateStore
from ..session.runtime import createSessionRuntimeController
from ..storage import configure_runtime_home, register_storage_lock_observer
from ..tasks.runtime import createTaskRuntime


MAX_STALE_DIAGNOSTICS = 128


async def bootstrapRuntime(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    values = {**(args or {}), **kwargs}
    config = values.get("config") or {}
    if config.get("runtimeHome"):
        configure_runtime_home(config["runtimeHome"])
    paths = getActiveRuntimePaths()
    return await runWithRuntimePaths(paths, lambda: _bootstrap_runtime(values, paths))


async def _bootstrap_runtime(args: dict[str, Any], paths: RuntimePaths) -> dict[str, Any]:
    session = args.get("session")
    config = args.get("config") or {}
    runtime_instance_id = str(uuid.uuid4())
    path_graph_id = _path_graph_id(paths)
    initial_session_id = (session or {}).get("id") or args.get("sessionId") or "runtime-unbound"
    active_binding = createRuntimeSessionBinding(initial_session_id, 1, 1, runtimeInstanceId=runtime_instance_id, pathGraphId=path_graph_id)
    generation = 1
    latest_activation_request = 1
    activation_counter_lock = threading.Lock()
    commit_lock = threading.Lock()
    state_lock = threading.RLock()
    lifecycle = createLifecycleBarrier()
    state_store = createRuntimeStateStore(createInitialRuntimeState(session) if session else None)
    legacy_subscribers: list[Callable[[str, dict[str, Any]], Any]] = []
    event_subscribers: list[Callable[[dict[str, Any]], Any]] = []
    stale_diagnostics: set[str] = set()
    pending_commit: dict[str, Any] | None = None
    retired_components: list[dict[str, Any]] = []
    runtime_api: dict[str, Any] = {}
    session_runtime: dict[str, Any] | None = None
    active_components: dict[str, Any] | None = None
    daemon = args.get("daemon") or args.get("daemonClient")
    owned_daemon, daemon_watch_stop = False, None
    daemon_unsubscribe: Any = None
    monitor_runtime: dict[str, Any] | None = args.get("monitorRuntime") or args.get("monitors")
    task_runtime: dict[str, Any] | None = args.get("taskRuntime") or args.get("tasks")
    collaboration_runtime: dict[str, Any] | None = args.get("collaborationRuntime")
    owned_monitor = monitor_runtime is None
    owned_task = task_runtime is None
    owned_collaboration = collaboration_runtime is None

    def publish(event: str, data: dict[str, Any]) -> None:
        typed_event = createRuntimeEvent({"event": event, "data": data, "defaultSessionId": active_binding.get("sessionId")})
        state_store["dispatch"](typed_event)
        envelope = {"event": event, "data": data, "typedEvent": typed_event}
        for listener in list(event_subscribers):
            listener(envelope)
        for listener in list(legacy_subscribers):
            listener(event, data)

    def report_stale(stale_binding: Mapping[str, Any] | None, event: str, stale_session_id: str | None = None) -> None:
        nonlocal stale_diagnostics
        with state_lock:
            key = f"{stale_session_id or (stale_binding or {}).get('sessionId') or 'unknown'}:{(stale_binding or {}).get('generation') or 'unknown'}:{event}"
            if key in stale_diagnostics:
                return
            if len(stale_diagnostics) >= MAX_STALE_DIAGNOSTICS:
                stale_diagnostics.pop()
            stale_diagnostics.add(key)
            current = active_binding
        publish(
            "runtime.stale_work_discarded",
            {
                "session_id": current.get("sessionId"),
                "stale_session_id": stale_session_id or (stale_binding or {}).get("sessionId"),
                "stale_generation": (stale_binding or {}).get("generation"),
                "active_generation": current.get("generation"),
                "runtime_instance_id": current.get("runtimeInstanceId"),
                "path_graph_id": current.get("pathGraphId"),
                "source_event": event,
            },
        )

    def dispatch_bound(binding: Mapping[str, Any], event: str, data: dict[str, Any] | None = None) -> None:
        normalized = {**(data or {}), "session_id": binding.get("sessionId")}
        with state_lock:
            if pending_commit and sameRuntimeBinding(binding, pending_commit["binding"]):
                pending_commit["events"].append((event, normalized))
                return
            stale = not sameRuntimeBinding(binding, active_binding)
        if stale:
            report_stale(binding, event)
            return
        publish(event, normalized)

    def dispatch(event: str, data: dict[str, Any] | None = None) -> None:
        payload = data or {}
        explicit_session_id = payload.get("session_id") or payload.get("sessionId")
        with state_lock:
            if pending_commit and explicit_session_id == pending_commit["binding"].get("sessionId"):
                pending_commit["events"].append((event, dict(payload)))
                return
            stale = bool(explicit_session_id and explicit_session_id != active_binding.get("sessionId"))
        if stale:
            report_stale(None, event, str(explicit_session_id))
            return
        publish(event, dict(payload))

    storage_lock_unsubscribe = register_storage_lock_observer(lambda payload: dispatch("storage.lock_timeout", dict(payload)))

    factories = _default_factories()

    async def prepare_components(next_session: dict[str, Any], next_config: dict[str, Any], binding: Mapping[str, Any], source: _StagedRuntimeSource, *, has_session: bool = True) -> dict[str, Any]:
        return await prepareRuntimeSessionComponents(
            {"binding": binding, "session": next_session, "config": next_config, "emit": source.emit, "factories": factories, "bootstrapArgs": {**args, "runtimePaths": paths}, "hasSession": has_session}
        )

    initial_source = _StagedRuntimeSource(active_binding, dispatch_bound)
    component_session = session or {"id": initial_session_id, "cwd": args.get("cwd") or config.get("cwd"), "metadata": {}}
    try:
        active_components = await prepare_components(component_session, config, active_binding, initial_source, has_session=session is not None)
        if session:
            session_runtime = createSessionRuntimeController({"session": session, "config": config, "binding": active_binding, "emitRuntimeEvent": dispatch})
        for event, data in initial_source.activate():
            dispatch_bound(active_binding, event, data)

        if daemon is None and args.get("daemonEnabled", True) is not False:
            daemon = await ensureTaskSupervisorDaemon({"instanceId": runtime_instance_id, "cwd": args.get("cwd") or config.get("cwd") or component_session.get("cwd")})
            owned_daemon = True
        if session and not daemon:
            if monitor_runtime is None:
                monitor_runtime = await createMonitorRuntime({"session": session, "config": config, "emit": dispatch, "runtimeBinding": active_binding, "runtimePaths": paths})
            if task_runtime is None:
                task_runtime = await createTaskRuntime({"session": session, "config": config, "emit": dispatch, "runtimeBinding": active_binding, "runtimePaths": paths, "startTicker": args.get("taskStartTicker", True)})
            if collaboration_runtime is None:
                collaboration_runtime = await createCollaborationRuntime({"session": session, "config": config, "emit": dispatch, "runtimeBinding": active_binding, "runtimePaths": paths})

        if daemon and session and _has_method(daemon, "subscribe"):
            initial_binding = active_binding
            daemon_unsubscribe = await _call_method(daemon, "subscribe", session["id"], lambda record: dispatch_bound(initial_binding, record.get("event"), record.get("data") or {}))
            if _has_method(daemon, "getStatus"):
                daemon_status = await _call_method(daemon, "getStatus")
                if daemon_status:
                    dispatch_bound(initial_binding, "daemon.snapshot", _daemon_snapshot_payload(daemon_status))
        if owned_daemon:
            async def _swap_daemon(client: dict[str, Any]) -> None:
                nonlocal daemon, daemon_unsubscribe
                with state_lock:
                    daemon = client
                    runtime_api["daemon"] = client
                previous_unsubscribe, daemon_unsubscribe = daemon_unsubscribe, None
                await _maybe_call(previous_unsubscribe)
                binding = active_binding
                if _has_method(client, "subscribe"):
                    daemon_unsubscribe = await _call_method(client, "subscribe", binding.get("sessionId"), lambda record: dispatch_bound(binding, record.get("event"), record.get("data") or {}))
            daemon_watch_stop = _start_daemon_watchdog({"instanceId": runtime_instance_id, "cwd": args.get("cwd") or config.get("cwd") or component_session.get("cwd"), "runtimePaths": paths, "emit": dispatch, "setDaemon": _swap_daemon})
    except BaseException:
        storage_lock_unsubscribe()
        initial_source.discard()
        await _close_many(
            [
                collaboration_runtime.get("close") if owned_collaboration and collaboration_runtime else None,
                task_runtime.get("close") if owned_task and task_runtime else None,
                monitor_runtime.get("close") if owned_monitor and monitor_runtime else None,
                active_components.get("close") if active_components else None,
            ],
        )
        raise

    async def prepare_activation(
        next_session: dict[str, Any],
        next_config: dict[str, Any],
        binding: Mapping[str, Any],
    ) -> dict[str, Any]:
        source = _StagedRuntimeSource(binding, dispatch_bound)
        components: dict[str, Any] | None = None
        unsubscribe: Any = None
        try:
            components = await prepare_components(next_session, next_config, binding, source)
            if daemon and _has_method(daemon, "subscribe"):
                unsubscribe = await _call_method(
                    daemon,
                    "subscribe",
                    next_session["id"],
                    lambda record: source.emit(record.get("event"), record.get("data") or {}),
                )
            return {
                "binding": binding,
                "session": next_session,
                "config": next_config,
                "components": components,
                "source": source,
                "daemonUnsubscribe": unsubscribe,
            }
        except BaseException:
            source.discard()
            await _maybe_call(unsubscribe)
            if components:
                await _maybe_call(components.get("close"))
            raise

    async def discard_prepared(prepared: dict[str, Any]) -> None:
        prepared["source"].discard()
        await _maybe_call(prepared.get("daemonUnsubscribe"))
        await _maybe_call(prepared["components"].get("close"))

    async def activate_projection(
        runtime: dict[str, Any] | None,
        next_session: dict[str, Any],
        next_config: dict[str, Any],
        binding: Mapping[str, Any],
    ) -> None:
        if not runtime:
            return
        if callable(runtime.get("activateSession")):
            await maybe_await(runtime["activateSession"](next_session, next_config, binding))
        elif callable(runtime.get("setSession")):
            await maybe_await(runtime["setSession"](next_session, next_config))

    async def activate_prepared(
        prepared: dict[str, Any], request: int
    ) -> Any:
        nonlocal active_binding, active_components, daemon_unsubscribe, pending_commit
        await _acquire_commit(commit_lock)
        try:
            with state_lock:
                superseded = request != latest_activation_request or lifecycle.state != "open"
            if superseded:
                await discard_prepared(prepared)
                raise RuntimeActivationSupersededError(prepared["session"]["id"])

            previous = {
                "binding": active_binding,
                "session": session_runtime["getActiveSession"]() if session_runtime else {},
                "config": session_runtime["getActiveConfig"]() if session_runtime else {},
                "components": active_components,
                "daemonUnsubscribe": daemon_unsubscribe,
            }
            with state_lock:
                pending_commit = {"binding": prepared["binding"], "events": []}
            failures: list[BaseException] = []
            for projection in (monitor_runtime, task_runtime, collaboration_runtime):
                try:
                    await activate_projection(
                        projection,
                        prepared["session"],
                        prepared["config"],
                        prepared["binding"],
                    )
                except BaseException as exc:
                    failures.append(exc)
            try:
                await prepared["components"]["activateDeferredWorkspace"]()
            except BaseException as exc:
                failures.append(exc)
            with state_lock:
                superseded = request != latest_activation_request or lifecycle.state != "open"
            if failures or superseded:
                with state_lock:
                    pending_commit = None
                for projection in (monitor_runtime, task_runtime, collaboration_runtime):
                    try:
                        await activate_projection(
                            projection,
                            previous["session"],
                            previous["config"],
                            previous["binding"],
                        )
                    except BaseException:
                        pass
                await discard_prepared(prepared)
                if superseded:
                    raise RuntimeActivationSupersededError(prepared["session"]["id"])
                raise _aggregate_runtime_failures("Runtime session activation failed.", failures)

            with state_lock:
                active_binding = prepared["binding"]
                active_components = prepared["components"]
                daemon_unsubscribe = prepared.get("daemonUnsubscribe")
            state_store["setState"](createInitialRuntimeState(prepared["session"]))
            if session_runtime:
                session_runtime["setActiveSession"](
                    prepared["session"], prepared["config"], prepared["binding"]
                )
            queued = prepared["source"].activate()
            with state_lock:
                queued.extend((pending_commit or {}).get("events") or [])
                pending_commit = None
            await _maybe_call(previous.get("daemonUnsubscribe"))
            activation_events = [item for item in queued if item[0] == "session.activated"]
            other_events = [item for item in queued if item[0] != "session.activated"]
            for event, data in [*activation_events, *other_events]:
                dispatch_bound(prepared["binding"], event, data)
            _install_component_surface(runtime_api, active_components)

            retired_components.append(previous["components"])
            retirement_status = "drained"
            try:
                await _maybe_call(previous["components"].get("close"))
                retired_components.remove(previous["components"])
            except BaseException as exc:
                retirement_status = "close_failed"
                publish(
                    "runtime.retired_components_close_failed",
                    {
                        "session_id": active_binding.get("sessionId"),
                        "retired_session_id": previous["binding"].get("sessionId"),
                        "reason": str(exc)[:2_048],
                    },
                )
            return createRuntimeActivationContext(
                {
                    "binding": prepared["binding"],
                    "session": prepared["session"],
                    "config": prepared["config"],
                    "tools": active_components["tools"],
                    "slashCommands": active_components["slashCommands"],
                    "workspace": active_components["workspace"],
                    "plugins": active_components["plugins"],
                    "mcp": active_components["mcp"],
                    "bridge": active_components["bridge"],
                    "retiredGeneration": {
                        "binding": previous["binding"],
                        "status": retirement_status,
                    },
                }
            )
        finally:
            commit_lock.release()

    def set_active_session(
        next_session: dict[str, Any], next_config: dict[str, Any]
    ) -> Any:
        nonlocal generation, latest_activation_request
        lifecycle.assertOpen("set active runtime session")
        with activation_counter_lock:
            generation += 1
            latest_activation_request += 1
            request = latest_activation_request
            binding = createRuntimeSessionBinding(
                next_session["id"],
                generation,
                request,
                runtimeInstanceId=runtime_instance_id,
                pathGraphId=path_graph_id,
            )

        async def operation() -> Any:
            prepared = await prepare_activation(next_session, next_config, binding)
            with state_lock:
                superseded = request != latest_activation_request
            if superseded:
                await discard_prepared(prepared)
                raise RuntimeActivationSupersededError(next_session["id"])
            return await activate_prepared(prepared, request)

        async def tracked() -> Any:
            return await lifecycle.run(
                "set active runtime session",
                lambda: runWithRuntimePaths(paths, operation),
            )

        coroutine = tracked()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        return loop.create_task(coroutine)

    async def apply_pending_mcp_catalog_changes() -> dict[str, Any]:
        captured_components = active_components
        captured_binding = active_binding
        result = await runWithRuntimePaths(paths, captured_components["refreshMcpTools"])
        if not sameRuntimeBinding(captured_binding, active_binding):
            report_stale(captured_binding, "mcp.tool_registry_refreshed")
            return {"changed": False, "servers": [], "generation": 0}
        if result.get("changed"):
            _install_component_surface(runtime_api, active_components)
        return {key: value for key, value in result.items() if key != "tools"}

    async def refresh_mcp_tool_registry() -> list[Any] | None:
        # The LIVE tool list on every call — a None on "unchanged" would send
        # later turns back to the startup snapshot captured in the bindings.
        captured = active_components
        await apply_pending_mcp_catalog_changes()
        return list(captured["tools"]) if captured is active_components else None

    def subscribe(listener: Callable[[str, dict[str, Any]], Any]) -> Callable[[], None]:
        lifecycle.assertOpen("subscribe to runtime events")
        legacy_subscribers.append(listener)
        return lambda: legacy_subscribers.remove(listener) if listener in legacy_subscribers else None

    def subscribe_events(listener: Callable[[dict[str, Any]], Any]) -> Callable[[], None]:
        lifecycle.assertOpen("subscribe to runtime events")
        event_subscribers.append(listener)
        return lambda: event_subscribers.remove(listener) if listener in event_subscribers else None

    def set_foreground_busy(busy: bool) -> None:
        lifecycle.assertOpen("set runtime foreground state")
        for projection in (monitor_runtime, task_runtime, collaboration_runtime):
            if projection and callable(projection.get("setForegroundBusy")):
                projection["setForegroundBusy"](busy)

    async def close() -> None:
        nonlocal latest_activation_request, daemon_unsubscribe
        with activation_counter_lock:
            latest_activation_request += 1

        async def cleanup() -> None:
            nonlocal daemon_unsubscribe
            if daemon_watch_stop:
                daemon_watch_stop()
            storage_lock_unsubscribe()
            await _maybe_call(daemon_unsubscribe)
            daemon_unsubscribe = None
            failures = await _close_many(
                [
                    (lambda: stopTaskSupervisorDaemon({"instanceId": runtime_instance_id})) if owned_daemon else _get_method(daemon, "close"),
                    collaboration_runtime.get("close") if owned_collaboration and collaboration_runtime else None,
                    task_runtime.get("close") if owned_task and task_runtime else None,
                    monitor_runtime.get("close") if owned_monitor and monitor_runtime else None,
                    active_components.get("close") if active_components else None,
                    *[component.get("close") for component in retired_components],
                ],
            )
            legacy_subscribers.clear()
            event_subscribers.clear()
            if failures:
                raise _aggregate_runtime_failures(
                    "One or more runtime components failed to close.",
                    failures,
                )

        from ..config._knobs import knobValue

        await runWithRuntimePaths(
            paths,
            lambda: lifecycle.close({"cleanup": cleanup, "failAfterMs": int(knobValue("runtime.close_failure_ms"))}),
        )
    runtime_api.update(
        {
            "daemon": daemon,
            "monitorRuntime": monitor_runtime,
            "monitors": monitor_runtime,
            "taskRuntime": task_runtime,
            "tasks": task_runtime,
            "collaborationRuntime": collaboration_runtime,
            "collaboration": collaboration_runtime,
            "emitEvent": dispatch,
            "emitRuntimeEvent": dispatch,
            "subscribe": subscribe,
            "subscribeEvents": subscribe_events,
            "getState": state_store["getState"],
            "stateStore": state_store,
            "getActiveBinding": lambda: active_binding,
            "setForegroundBusy": set_foreground_busy,
            "applyPendingMcpCatalogChanges": apply_pending_mcp_catalog_changes,
            "refreshMcpToolRegistry": refresh_mcp_tool_registry,
            "getMonitorSnapshot": lambda: state_store["getState"]()
            .get("monitors", {})
            .get("snapshot"),
            "getTaskSnapshot": lambda: task_runtime["getSnapshot"]()
            if task_runtime and callable(task_runtime.get("getSnapshot"))
            else _task_snapshot_from_state(state_store["getState"]),
            "setActiveSession": set_active_session,
            "startTurn": session_runtime["startTurn"] if session_runtime else lambda *_a, **_k: None, "setTurnCancelling": session_runtime["setTurnCancelling"] if session_runtime else lambda: None,
            "finishTurn": session_runtime["finishTurn"] if session_runtime else lambda *_a, **_k: None,
            "failTurn": session_runtime["failTurn"] if session_runtime else lambda _error: None,
            "isTurnInFlight": session_runtime["isTurnInFlight"] if session_runtime else lambda: False,
            "close": close,
            "runtimePaths": paths,
            "runtimeInstanceId": runtime_instance_id,
            "pathGraphId": path_graph_id,
        }
    )
    _install_component_surface(runtime_api, active_components)
    return runtime_api
