from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

from tests.support.agent_core_case import *
from agent_core.storage.persisted_record_scanner import (
    DEFAULT_MAX_SCAN_BYTES,
    DEFAULT_MAX_SCAN_ENTRIES,
    DEFAULT_READ_CONCURRENCY,
    FAMILY_MAX_BYTES,
    getPersistedRecordIssue,
    listPersistedRecordIssues,
    scanPersistedJsonRecords,
    setPersistedRecordBeforeOpenObserverForTesting,
    setPersistedRecordReadObserverForTesting,
)


class PersistedRecordScannerTests(AgentCoreTestCase):
    def test_scalar_record_reads_retry_concurrent_atomic_replacement(self):
        root, home, _workspace, previous = self.temp_context()
        try:
            core.configureRuntimeHome(str(home))
            run(core.ensureBaseLayout(str(home)))
            state_path = Path(core.daemonStatePath("rc-instance"))
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({"pid": 101, "phase": "starting"}), encoding="utf-8")
            replacements = []
            for pid in (202, 303, 404):
                replacement = Path(f"{state_path}.replacement-{pid}")
                replacement.write_text(json.dumps({"pid": pid, "phase": "running"}), encoding="utf-8")
                replacements.append(replacement)
            replacement_count = 0

            def replace_before_open(file_path: str) -> None:
                nonlocal replacement_count
                if file_path != str(state_path) or replacement_count >= len(replacements):
                    return
                os.replace(replacements[replacement_count], state_path)
                replacement_count += 1

            setPersistedRecordBeforeOpenObserverForTesting(replace_before_open)
            state = run(core.loadDaemonState("rc-instance"))
            self.assertEqual(replacement_count, 3)
            self.assertEqual(state["pid"], 404)
            self.assertIsNone(getPersistedRecordIssue(str(state_path)))
        finally:
            setPersistedRecordBeforeOpenObserverForTesting(None)
            self.restore_home(root, previous)

    def test_mixed_malformed_and_oversized_stores_isolate_failures(self):
        root, home, workspace, previous = self.temp_context()
        runtime = None
        reads: list[tuple[str, int, int]] = []
        try:
            core.configureRuntimeHome(str(home))
            run(core.ensureBaseLayout(str(workspace)))
            session = core.createEmptySession(
                cwd=str(workspace),
                provider="openai",
                model="test-model",
                systemPrompt="test",
                permissionMode="default",
                sessionId="corruption-session",
            )
            session["messages"] = [{"role": "user", "content": "recover healthy work"}]
            session["metadata"]["taskIds"] = [
                "task-valid",
                "task-malformed",
                "task-oversized",
                "task-future",
            ]
            run(core.saveSession(session))
            now = "2026-07-16T00:00:00Z"
            run(core.writeTaskMeta("task-valid", {
                "taskId": "task-valid",
                "sessionId": session["id"],
                "type": "background_command",
                "status": "completed",
                "startedAt": now,
                "updatedAt": now,
            }))
            run(core.writeTaskMeta("task-future", {
                "taskId": "task-future",
                "sessionId": session["id"],
                "type": "future_quantum_task",
                "status": "future_waiting",
            }))
            malformed_task = Path(core.taskMetaPath("task-malformed"))
            malformed_task.parent.mkdir(parents=True, exist_ok=True)
            malformed_task.write_text("{", encoding="utf-8")
            _make_sparse(Path(core.taskMetaPath("task-oversized")), 2 * 1024 * 1024)

            run(core.saveChildAgent(_child_agent("agent-valid", session["id"], "backgrounded", now)))
            run(core.saveChildAgent(_child_agent("agent-future", session["id"], "future_status", now)))
            Path(core.childAgentPath("agent-malformed")).write_text("{", encoding="utf-8")
            _make_sparse(Path(core.childAgentPath("agent-oversized")), 2 * 1024 * 1024)

            monitor = core.createMonitorRecord({
                "id": "monitor-valid",
                "sessionId": session["id"],
                "taskId": "task-valid",
                "goal": "watch valid work",
            })
            run(core.saveMonitor(monitor))
            session["metadata"]["monitorIds"] = ["monitor-valid", "monitor-malformed", "monitor-oversized"]
            Path(core.monitorPath("monitor-malformed")).write_text("{", encoding="utf-8")
            _make_sparse(Path(core.monitorPath("monitor-oversized")), 2 * 1024 * 1024)

            health = {
                "taskId": "health-valid",
                "sessionId": session["id"],
                "type": "health_check",
                "mode": "command",
                "status": "stopped",
                "createdAt": now,
                "updatedAt": now,
                "command": "true",
            }
            run(core.saveHealthCheckTask(health))
            Path(core.healthCheckMetaPath("health-malformed")).parent.mkdir(parents=True, exist_ok=True)
            Path(core.healthCheckMetaPath("health-malformed")).write_text("{", encoding="utf-8")
            _make_sparse(Path(core.healthCheckMetaPath("health-oversized")), 2 * 1024 * 1024)
            Path(core.daemonStatePath("rc-instance")).parent.mkdir(parents=True, exist_ok=True)
            Path(core.daemonStatePath("rc-instance")).write_text("{", encoding="utf-8")

            oversized_paths = [
                Path(core.taskMetaPath("task-oversized")),
                Path(core.childAgentPath("agent-oversized")),
                Path(core.monitorPath("monitor-oversized")),
                Path(core.healthCheckMetaPath("health-oversized")),
            ]
            sizes_before = [path.stat().st_size for path in oversized_paths]
            setPersistedRecordReadObserverForTesting(
                lambda file_path, requested, actual: reads.append((file_path, requested, actual))
            )

            agents, monitors, health_checks = run(_gather_records())
            self.assertEqual(sorted(record["agentId"] for record in agents), ["agent-future", "agent-valid"])
            self.assertEqual([record["id"] for record in monitors], ["monitor-valid"])
            self.assertEqual([record["taskId"] for record in health_checks], ["health-valid"])
            self.assertIsNone(run(core.loadDaemonState("rc-instance")))
            for path in oversized_paths:
                self.assertFalse(any(file_path == str(path) for file_path, _requested, _actual in reads))

            issues = listPersistedRecordIssues(None, str(home))
            self.assertTrue(any(issue["reason"] == "malformed_json" for issue in issues))
            self.assertTrue(any(issue["reason"] == "record_too_large" for issue in issues))
            self.assertTrue(all(len(issue.get("detail", "")) <= 240 for issue in issues))

            events: list[tuple[str, dict[str, Any]]] = []
            runtime = run(core.createTaskRuntime({
                "session": session,
                "config": {"cwd": str(workspace), "runtimeHome": str(home)},
                "emit": lambda event, data: events.append((event, data)),
                "startTicker": False,
            }))
            self.assertTrue(any(event == "task.registered" and data["task_id"] == "task-valid" for event, data in events))
            self.assertTrue(any(
                event == "task.projection_skipped"
                and data["task_id"] == "task-oversized"
                and data["reason"] == "record_too_large"
                for event, data in events
            ))
            self.assertTrue(any(
                event == "task.projection_skipped"
                and data["task_id"] == "task-future"
                and "unknown task meta type" in data["reason"]
                for event, data in events
            ))
            self.assertEqual(session["metadata"]["taskIds"], [
                "task-valid",
                "task-malformed",
                "task-oversized",
                "task-future",
            ])
            run(runtime["close"]())
            runtime = None

            self.assertEqual([path.stat().st_size for path in oversized_paths], sizes_before)
            quarantine_names = [
                path.name
                for path in home.rglob("*")
                if any(marker in path.name.lower() for marker in (".corrupt-", ".quarantine", ".backup"))
            ]
            self.assertEqual(quarantine_names, [])
        finally:
            setPersistedRecordReadObserverForTesting(None)
            if runtime:
                run(runtime["close"]())
            self.restore_home(root, previous)

    def test_scans_bound_entries_bytes_concurrency_and_symlinks(self):
        root, home, _workspace, previous = self.temp_context()
        try:
            directory = home / "scan"
            directory.mkdir()
            for index in range(12):
                (directory / f"agent-{index}.json").write_text(json.dumps({"index": index}), encoding="utf-8")
            symlink = directory / "agent-link.json"
            symlink.symlink_to(directory / "agent-0.json")
            select = lambda entry: {"id": entry.name, "filePath": str(directory / entry.name)}
            validate = lambda value, _candidate: isinstance(value, dict)

            entry_limited = run(scanPersistedJsonRecords(
                family="agent",
                directory=str(directory),
                select=select,
                validate=validate,
                maxEntries=3,
                concurrency=2,
            ))
            self.assertTrue(entry_limited["truncated"])
            self.assertLessEqual(entry_limited["examinedEntries"], 4)
            self.assertTrue(any(issue["reason"] == "scan_entry_limit" for issue in entry_limited["issues"]))

            byte_limited = run(scanPersistedJsonRecords(
                family="agent",
                directory=str(directory),
                select=select,
                validate=validate,
                maxEntries=20,
                maxTotalBytes=24,
                concurrency=2,
            ))
            self.assertTrue(byte_limited["truncated"])
            self.assertLessEqual(byte_limited["admittedBytes"], 24)
            self.assertTrue(any(issue["reason"] == "scan_byte_limit" for issue in byte_limited["issues"]))
            self.assertTrue(any(issue["reason"] == "not_regular_file" for issue in byte_limited["issues"]))

            active = 0
            maximum_active = 0
            lock = threading.Lock()

            def observe_before_open(_file_path: str) -> None:
                nonlocal active, maximum_active
                with lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                time.sleep(0.01)
                with lock:
                    active -= 1

            setPersistedRecordBeforeOpenObserverForTesting(observe_before_open)
            run(scanPersistedJsonRecords(
                family="agent",
                directory=str(directory),
                select=select,
                validate=validate,
                maxEntries=20,
                concurrency=2,
            ))
            self.assertEqual(maximum_active, 2)
            self.assertEqual(DEFAULT_MAX_SCAN_ENTRIES, 4_096)
            self.assertEqual(DEFAULT_MAX_SCAN_BYTES, 64 * 1024 * 1024)
            self.assertEqual(DEFAULT_READ_CONCURRENCY, 8)
            self.assertEqual(min(FAMILY_MAX_BYTES.values()), 256 * 1024)
            self.assertEqual(max(FAMILY_MAX_BYTES.values()), 4 * 1024 * 1024)
        finally:
            setPersistedRecordBeforeOpenObserverForTesting(None)
            self.restore_home(root, previous)

    def test_rejected_corruption_reporting_leaves_no_periodic_work(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            core.configureRuntimeHome(str(home))
            run(core.ensureBaseLayout(str(workspace)))
            malformed = Path(core.childAgentPath("agent-malformed"))
            malformed.write_text("{", encoding="utf-8")
            calls = 0

            def reject(event: dict[str, Any]) -> None:
                nonlocal calls
                if event["event"] == "storage.record_unavailable":
                    calls += 1
                    raise RuntimeError("reject corruption diagnostic")

            with self.assertRaisesRegex(RuntimeError, "reject corruption diagnostic"):
                run(core.createTaskSupervisor({"cwd": str(workspace), "onEvent": reject}))
            calls_after_reject = calls
            time.sleep(0.1)
            self.assertEqual(calls, calls_after_reject)

            events: list[dict[str, Any]] = []
            supervisor = run(core.createTaskSupervisor({
                "cwd": str(workspace),
                "onEvent": events.append,
            }))
            self.assertTrue(any(event["event"] == "storage.record_unavailable" for event in events))
            run(supervisor["close"]())
            supervisor = None
        finally:
            if supervisor:
                run(supervisor["close"]())
            self.restore_home(root, previous)


async def _gather_records() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    return tuple(await asyncio.gather(
        core.listChildAgents(),
        core.listMonitors(),
        core.listHealthCheckTasks(),
    ))


def _child_agent(agent_id: str, parent_session_id: str, status: str, started_at: str) -> dict[str, Any]:
    return {
        "agentId": agent_id,
        "sessionId": f"child-{agent_id}",
        "parentSessionId": parent_session_id,
        "provider": "openai",
        "model": "test-model",
        "mode": "default",
        "runInBackground": True,
        "status": status,
        "startedAt": started_at,
    }


def _make_sparse(file_path: Path, size: int) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("wb") as handle:
        handle.truncate(size)
