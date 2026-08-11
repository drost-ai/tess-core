from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path
from unittest import mock

import agent_core.tasks.supervisor as supervisor_module
import agent_core.process._termination as process_termination_module
import agent_core.tasks.supervisor_parts.stop as supervisor_stop_module
import agent_core.tasks.runners.background as background_runner_module
from agent_core.tasks.task_stop_coordination import (
    TaskStopCoordinator,
    set_task_stop_coordination_test_hook_for_tests,
)
from agent_core.tasks.terminal_store import (
    commit_prepared_task_stop,
    commit_task_process_completion,
    prepare_task_stop,
)
from agent_core.tasks.terminal_transitions import (
    apply_deliberate_task_stop,
    apply_task_process_completion,
)
from tests.support.agent_core_case import AgentCoreTestCase, core, run


class Package31TaskStopTerminalTests(AgentCoreTestCase):
    def _session(self, home: Path, workspace: Path):
        config = run(
            core.resolve_config(
                {
                    "provider": "openai",
                    "runtimeHome": str(home),
                    "permissionMode": "bypassPermissions",
                },
                str(workspace),
            )
        )
        session = core.create_empty_session(
            cwd=str(workspace),
            provider=config["provider"],
            providerProfileName=config["providerProfileName"],
            model=config["model"],
            effort=config["effort"],
            systemPrompt=config["systemPrompt"],
            permissionMode="bypassPermissions",
        )
        run(core.save_session(session))
        return config, session

    @staticmethod
    def _identity(pid: int = 4242) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "pid": pid,
            "processGroupId": pid,
            "spawnedAt": "2026-07-17T00:00:00Z",
            "startToken": "test:task-stop",
            "observedCommandDigest": "a" * 64,
            "expectedCommandDigest": "b" * 64,
            "platform": sys.platform,
        }

    def _seed_process_task(self, task_id: str, session_id: str) -> dict[str, object]:
        identity = self._identity()
        stamp = "2026-07-17T00:00:00Z"
        run(
            core.save_task_record(
                {
                    "id": task_id,
                    "sessionId": session_id,
                    "sourceId": task_id,
                    "kind": "background_command",
                    "status": "backgrounded",
                    "title": "Long task",
                    "summary": "Task is running.",
                    "createdAt": stamp,
                    "updatedAt": stamp,
                    "startedAt": stamp,
                    "cwd": "/tmp",
                    "command": "sleep 10",
                    "owner": "daemon",
                    "processIdentity": identity,
                }
            )
        )
        run(
            core.write_task_meta(
                task_id,
                {
                    "type": "background_command",
                    "taskId": task_id,
                    "id": task_id,
                    "sessionId": session_id,
                    "status": "running",
                    "title": "Long task",
                    "startedAt": stamp,
                    "updatedAt": stamp,
                    "cwd": "/tmp",
                    "command": "sleep 10",
                    "processIdentity": identity,
                },
            )
        )
        return identity

    def test_transition_matrix_distinguishes_expected_and_unexpected_loss(self):
        stopped = apply_deliberate_task_stop("running")
        self.assertEqual(
            (stopped["status"], stopped["changed"], stopped["event"]),
            ("stopped", True, "task.stopped"),
        )
        for terminal in ("completed", "failed", "stopped"):
            with self.subTest(terminal=terminal):
                transition = apply_deliberate_task_stop(terminal)
                self.assertEqual(
                    (transition["previousStatus"], transition["status"], transition["changed"]),
                    (terminal, terminal, False),
                )

        expected = apply_task_process_completion(
            current="running",
            code=None,
            signalName="SIGTERM",
            terminationIntent={"signal": "SIGTERM"},
        )
        unexpected = apply_task_process_completion(
            current="running",
            code=None,
            signalName="SIGTERM",
            terminationIntent=None,
        )
        escalated = apply_task_process_completion(
            current="running",
            code=None,
            signalName="SIGKILL",
            terminationIntent={"signal": "SIGTERM"},
        )
        self.assertEqual((expected["status"], expected["event"]), ("stopped", "task.stopped"))
        self.assertEqual((unexpected["status"], unexpected["event"]), ("failed", "task.failed"))
        self.assertEqual((escalated["status"], escalated["event"]), ("stopped", "task.stopped"))

    def test_concurrent_supervisor_stops_emit_once_and_retry_is_byte_stable(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            _config, session = self._session(home, workspace)
            events: list[dict[str, object]] = []
            supervisor = run(
                core.create_task_supervisor(
                    {"cwd": str(workspace), "onEvent": lambda event: events.append(event)}
                )
            )
            task_id = "task-stop-concurrent"
            identity = self._seed_process_task(task_id, session["id"])
            session["metadata"]["taskIds"] = [task_id]

            async def signal_stub(value, signum):
                self.assertEqual(value, identity)
                self.assertEqual(int(signum), 15)
                await asyncio.sleep(0.01)
                return value

            async def stop_many():
                return await asyncio.gather(
                    *(supervisor["stopTask"](task_id) for _ in range(50))
                )

            with mock.patch.object(supervisor_stop_module, "signalOwnedProcess", signal_stub), mock.patch.object(process_termination_module, "signalOwnedProcess", signal_stub):
                results = run(stop_many())
            self.assertEqual(sum(bool(result and result["changed"]) for result in results), 1)
            self.assertTrue(all(result and result["status"] == "stopped" for result in results))
            stopped_events = [event for event in events if event["event"] == "task.stopped"]
            self.assertEqual(len(stopped_events), 1)
            self.assertFalse(any(event["event"] == "task.failed" for event in events))
            transition_ids = {
                result.get("transitionId") for result in results if result and result.get("transitionId")
            }
            self.assertEqual(len(transition_ids), 1)

            record_path = Path(core.task_record_path(task_id))
            meta_path = Path(core.task_meta_path(task_id))
            before = (
                record_path.read_bytes(),
                record_path.stat().st_mtime_ns,
                meta_path.read_bytes(),
                meta_path.stat().st_mtime_ns,
                len(events),
            )
            retry = run(supervisor["stopTask"](task_id))
            after = (
                record_path.read_bytes(),
                record_path.stat().st_mtime_ns,
                meta_path.read_bytes(),
                meta_path.stat().st_mtime_ns,
                len(events),
            )
            self.assertEqual((retry["status"], retry["changed"]), ("stopped", False))
            self.assertEqual(after, before)
        finally:
            if supervisor:
                run(supervisor["close"]())
            self.restore_home(root, previous)

    def test_process_finalizer_waits_for_caller_commit(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _config, session = self._session(home, workspace)
            task_id = "task-stop-caller-first"
            self._seed_process_task(task_id, session["id"])
            prepared = run(prepare_task_stop(task_id, "SIGTERM"))
            self.assertIsNotNone(prepared)
            coordinator = TaskStopCoordinator()
            release = coordinator.holdFinalization(task_id)
            finalizer_waiting = threading.Event()
            finalizer_result: list[dict[str, object]] = []

            def hook(stage: str, observed_task_id: str) -> None:
                self.assertEqual(observed_task_id, task_id)
                if stage == "finalizer_waiting":
                    finalizer_waiting.set()

            def finalize() -> None:
                coordinator.waitForStopCommit(task_id)
                finalizer_result.append(
                    run(
                        commit_task_process_completion(
                            taskId=task_id,
                            code=None,
                            signalName="SIGTERM",
                        )
                    )
                )

            set_task_stop_coordination_test_hook_for_tests(hook)
            thread = threading.Thread(target=finalize, daemon=True)
            thread.start()
            self.assertTrue(finalizer_waiting.wait(2))
            committed = run(
                commit_prepared_task_stop(task_id, prepared["transitionId"])
            )
            release()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertTrue(committed["transition"]["changed"])
            self.assertEqual(finalizer_result[0]["transition"]["changed"], False)
            self.assertEqual(run(core.load_task_record(task_id))["status"], "stopped")
        finally:
            set_task_stop_coordination_test_hook_for_tests(None)
            self.restore_home(root, previous)

    def test_runner_first_adopts_prepared_transition(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _config, session = self._session(home, workspace)
            task_id = "task-stop-runner-first"
            self._seed_process_task(task_id, session["id"])
            prepared = run(prepare_task_stop(task_id, "SIGTERM"))
            meta = run(core.read_task_meta(task_id))
            run(
                core.write_task_meta(
                    task_id,
                    {
                        **meta,
                        "status": "terminated",
                        "terminalTransitionId": prepared["transitionId"],
                        "terminalEventOwner": "supervisor",
                        "finishedAt": "2026-07-17T00:00:01Z",
                    },
                )
            )
            committed = run(
                commit_prepared_task_stop(task_id, prepared["transitionId"])
            )
            self.assertTrue(committed["transition"]["changed"])
            self.assertEqual(committed["result"]["transitionId"], prepared["transitionId"])
            record = run(core.load_task_record(task_id))
            self.assertEqual(record["status"], "stopped")
            self.assertEqual(
                record["metadata"]["terminal_transition_id"],
                prepared["transitionId"],
            )
        finally:
            self.restore_home(root, previous)

    def test_background_runner_uses_durable_intent_for_signal_semantics(self):
        root, home, workspace, previous = self.temp_context()
        try:
            self._session(home, workspace)

            class FakeProcess:
                pid = 4242

                def wait(self):
                    return -15

                def poll(self):
                    return None

            for suffix, intent, expected_status, expected_owner in (
                (
                    "expected",
                    {
                        "schemaVersion": 1,
                        "transitionId": "prepared-transition",
                        "requestedAt": "2026-07-17T00:00:00Z",
                        "signal": "SIGTERM",
                        "eventOwner": "supervisor",
                    },
                    "terminated",
                    "supervisor",
                ),
                ("unexpected", None, "failed", "task_runtime"),
            ):
                with self.subTest(suffix=suffix):
                    task_id = f"runner-signal-{suffix}"
                    initial = {
                        "type": "background_command",
                        "taskId": task_id,
                        "id": task_id,
                        "sessionId": "runner-session",
                        "status": "running",
                        "startedAt": "2026-07-17T00:00:00Z",
                    }
                    if intent:
                        initial["terminationIntent"] = intent
                    run(core.write_task_meta(task_id, initial))
                    output_path = workspace / f"{suffix}.log"
                    with mock.patch.object(
                        background_runner_module.subprocess,
                        "Popen",
                        return_value=FakeProcess(),
                    ):
                        run(
                            background_runner_module.run_background_command(
                                {
                                    "taskId": task_id,
                                    "command": "ignored",
                                    "cwd": str(workspace),
                                    "outputPath": str(output_path),
                                    "shell": "/bin/sh",
                                }
                            )
                        )
                    meta = run(core.read_task_meta(task_id))
                    self.assertEqual(meta["status"], expected_status)
                    self.assertEqual(meta["terminalEventOwner"], expected_owner)
                    self.assertIsNone(meta.get("exitCode"))
                    if intent:
                        self.assertEqual(
                            meta["terminalTransitionId"], intent["transitionId"]
                        )
                        self.assertNotIn("error", meta)
                    else:
                        self.assertEqual(meta["signal"], "SIGTERM")
                        self.assertIn("unexpected SIGTERM", meta["error"])
        finally:
            self.restore_home(root, previous)

    def test_health_stop_is_neutral_idempotent_and_event_exact(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            _config, session = self._session(home, workspace)
            events: list[dict[str, object]] = []
            supervisor = run(
                core.create_task_supervisor(
                    {"cwd": str(workspace), "onEvent": lambda event: events.append(event)}
                )
            )
            task_id = "health-stop-neutral"
            stamp = "2026-07-17T00:00:00Z"
            health = {
                "type": "health_check",
                "taskId": task_id,
                "id": task_id,
                "sessionId": session["id"],
                "title": "Health",
                "mode": "http",
                "url": "https://example.invalid/health",
                "status": "active",
                "createdAt": stamp,
                "startedAt": stamp,
                "updatedAt": stamp,
                "revision": 1,
                "generation": 1,
            }
            run(core.save_health_check_task(health))
            run(core.write_task_meta(task_id, health))
            run(
                core.save_task_record(
                    {
                        "id": task_id,
                        "sessionId": session["id"],
                        "sourceId": task_id,
                        "kind": "health_check",
                        "status": "running",
                        "title": "Health",
                        "summary": "Health check is running.",
                        "createdAt": stamp,
                        "updatedAt": stamp,
                        "startedAt": stamp,
                    }
                )
            )
            first = run(supervisor["stopTask"](task_id))
            stopped_bytes = Path(core.task_record_path(task_id)).read_bytes()
            second = run(supervisor["stopTask"](task_id))
            self.assertEqual((first["status"], first["changed"]), ("stopped", True))
            self.assertEqual((second["status"], second["changed"]), ("stopped", False))
            self.assertEqual(Path(core.task_record_path(task_id)).read_bytes(), stopped_bytes)
            self.assertEqual([event["event"] for event in events], ["task.stopped"])
            self.assertEqual(run(core.load_health_check_task(task_id))["status"], "stopped")
        finally:
            if supervisor:
                run(supervisor["close"]())
            self.restore_home(root, previous)

    def test_terminal_task_stop_skips_permission_signal_and_failure_event(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config, session = self._session(home, workspace)
            task_id = "task-stop-terminal-noop"
            stamp = "2026-07-17T00:00:00Z"
            session["metadata"]["taskIds"] = [task_id]
            run(
                core.save_task_record(
                    {
                        "id": task_id,
                        "sessionId": session["id"],
                        "sourceId": task_id,
                        "kind": "background_command",
                        "status": "completed",
                        "title": "Done",
                        "summary": "Done",
                        "createdAt": stamp,
                        "updatedAt": stamp,
                        "completedAt": stamp,
                    }
                )
            )
            run(
                core.write_task_meta(
                    task_id,
                    {
                        "type": "background_command",
                        "taskId": task_id,
                        "sessionId": session["id"],
                        "status": "completed",
                        "startedAt": stamp,
                        "finishedAt": stamp,
                    },
                )
            )
            tool = next(tool for tool in core.create_builtin_tools() if tool.name == "TaskStop")
            events: list[tuple[str, dict[str, object]]] = []
            context = {
                "session": session,
                "config": config,
                "interactive": False,
                "askQuestion": lambda *_: "",
                "outputJson": lambda event, data: events.append((event, data)),
                "cwd": str(workspace),
            }
            with mock.patch(
                "agent_core.tools.tasks._ensure_tool_permission",
                new=mock.AsyncMock(),
            ) as permission, mock.patch(
                "agent_core.tools.tasks.signalOwnedProcess",
                new=mock.AsyncMock(),
            ) as signal_process:
                payload = json.loads(run(tool.handler({"task_id": task_id}, context))["content"])
            self.assertEqual(
                payload,
                {
                    "task_id": task_id,
                    "previous_status": "completed",
                    "status": "completed",
                    "changed": False,
                    "stopped": False,
                    "note": "Task was already terminal; no process signal was sent.",
                },
            )
            permission.assert_not_awaited()
            signal_process.assert_not_awaited()
            self.assertEqual(events, [])
        finally:
            self.restore_home(root, previous)


if __name__ == "__main__":
    import unittest

    unittest.main()
