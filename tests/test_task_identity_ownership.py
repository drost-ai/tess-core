from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
import sys

from tests.support.agent_core_case import *
from agent_core._utils import now_iso
import agent_core.process.identity as process_identity_module


ACCEPTED = {
    "session": ["6b4ec4c6-00ae-4c2f-94a7-5ccb3ef242c8", "session-1", "child_session-2"],
    "task": ["37b91f36-13ed-4fd0-bf0f-8714fbd3d078", "task-1", "background_task"],
    "monitor": ["9dd4cf06-2710-4fdb-a912-3b603d2eb62d", "monitor-1"],
    "agent": ["agent-46e123a0-f9d4-4687-a98a-8bb3dd022ef4", "agent-test"],
    "bridge": ["b6706ba8-8321-4d1e-b234-77675a006f62", "bridge-1"],
    "remote": ["070c361c-f597-4e3c-af55-99e2c2079ad8", "remote-1"],
}
REJECTED = (
    "",
    ".",
    "..",
    "../escape",
    "a/../../escape",
    "a\\b",
    "a..b",
    "-leading",
    "trailing-",
    "has space",
    "monitor:uuid",
    "nul\0byte",
    "line\nbreak",
    "snowman-☃",
    "a" * 129,
)


class TaskIdentityOwnershipTests(AgentCoreTestCase):
    def tearDown(self):
        getattr(process_identity_module, "__setProcessIdentityObserverForTests")(None)

    def test_runtime_ids_accept_opaque_ids_and_reject_path_bearing_forms(self):
        parsers = (
            (core.parseSessionId, ACCEPTED["session"]),
            (core.parseTaskId, ACCEPTED["task"]),
            (core.parseMonitorId, ACCEPTED["monitor"]),
            (core.parseAgentId, ACCEPTED["agent"]),
            (core.parseBridgeId, ACCEPTED["bridge"]),
            (core.parseRemoteSessionId, ACCEPTED["remote"]),
        )
        for parser, accepted in parsers:
            for value in accepted:
                self.assertEqual(parser(value), value)
            for value in REJECTED:
                with self.subTest(parser=parser.__name__, value=value):
                    with self.assertRaises(core.RuntimeIdentifierError):
                        parser(value)

    def test_monitor_task_reference_has_one_explicit_grammar(self):
        monitor_id = ACCEPTED["monitor"][0]
        self.assertEqual(
            core.parseTaskReference(f"monitor:{monitor_id}"),
            {"family": "monitor", "id": monitor_id},
        )
        self.assertEqual(core.formatMonitorTaskReference(monitor_id), f"monitor:{monitor_id}")
        self.assertEqual(core.parseTaskReference("task-1"), {"family": "task", "id": "task-1"})
        with self.assertRaises(core.RuntimeIdentifierError):
            core.parseTaskReference("remote:uuid")
        with self.assertRaises(core.RuntimeIdentifierError):
            core.parseTaskReference("monitor:../escape")

    def test_every_record_path_constructor_rejects_traversal_before_join(self):
        root, home, workspace, previous = self.temp_context()
        try:
            core.configure_runtime_home(str(home))
            constructors = (
                core.task_dir,
                core.task_output_path,
                core.task_record_path,
                core.monitor_path,
                core.child_agent_path,
                core.remote_session_path,
                core.bridge_path,
                core.bridge_inbox_path,
                core.bridge_events_path,
                core.session_path,
                core.session_archive_dir,
            )
            for constructor in constructors:
                for value in ("../../outside", "nested/child"):
                    with self.subTest(constructor=constructor.__name__, value=value):
                        with self.assertRaises(core.RuntimeIdentifierError):
                            constructor(value)
        finally:
            self.restore_home(root, previous)

    def test_record_scans_ignore_hostile_filenames(self):
        root, home, workspace, previous = self.temp_context()
        try:
            core.configure_runtime_home(str(home))
            run(core.ensure_base_layout(str(workspace)))
            now = now_iso()
            run(
                core.save_task_record(
                    {
                        "id": "safe-task",
                        "sessionId": "session-1",
                        "sourceId": "safe-task",
                        "kind": "background_command",
                        "status": "completed",
                        "title": "safe",
                        "createdAt": now,
                        "updatedAt": now,
                        "startedAt": now,
                        "foregroundable": True,
                        "restorable": True,
                    }
                )
            )
            records = home / "task-records"
            (records / "hostile.name.json").write_text("not-json", encoding="utf-8")
            self.assertEqual([record["id"] for record in run(core.list_task_records())], ["safe-task"])
        finally:
            self.restore_home(root, previous)

    def test_task_operations_require_session_ownership_reference_and_family(self):
        root, home, workspace, previous = self.temp_context()
        try:
            core.configure_runtime_home(str(home))
            run(core.ensure_base_layout(str(workspace)))
            now = now_iso()

            def record(task_id: str, session_id: str, kind: str):
                return {
                    "id": task_id,
                    "sessionId": session_id,
                    "sourceId": task_id,
                    "kind": kind,
                    "status": "running",
                    "title": task_id,
                    "createdAt": now,
                    "updatedAt": now,
                    "startedAt": now,
                    "foregroundable": True,
                    "restorable": True,
                }

            run(core.save_task_record(record("task-owned", "session-owner", "background_command")))
            run(
                core.write_task_meta(
                    "task-owned",
                    {"type": "background_command", "taskId": "task-owned", "sessionId": "session-owner", "status": "running"},
                )
            )
            attacker = {"id": "session-attacker", "metadata": {"taskIds": ["task-owned"]}}
            with self.assertRaises(core.TaskOwnershipError) as foreign:
                run(core.requireModelOwnedTask({"session": attacker}, "task-owned", "output"))
            self.assertEqual(foreign.exception.code, "task.not_owned")
            with self.assertRaises(core.RuntimeIdentifierError):
                run(core.requireModelOwnedTask({"session": attacker}, "../../task-owned", "output"))

            owner = {"id": "session-owner", "metadata": {"taskIds": []}}
            with self.assertRaises(core.TaskOwnershipError) as unreferenced:
                run(core.requireModelOwnedTask({"session": owner}, "task-owned", "output"))
            self.assertEqual(unreferenced.exception.code, "task.not_referenced")
            owner["metadata"]["taskIds"].append("task-owned")
            self.assertEqual(
                run(core.requireModelOwnedTask({"session": owner}, "task-owned", "output"))["id"],
                "task-owned",
            )

            run(core.save_task_record(record("task-remote", "session-owner", "remote")))
            run(core.write_task_meta("task-remote", {"type": "remote", "taskId": "task-remote", "sessionId": "session-owner"}))
            owner["metadata"]["taskIds"].append("task-remote")
            with self.assertRaises(core.TaskOwnershipError) as wrong_kind:
                run(core.requireModelOwnedTask({"session": owner}, "task-remote", "output"))
            self.assertEqual(wrong_kind.exception.code, "task.kind_mismatch")
        finally:
            self.restore_home(root, previous)

    def test_monitor_requires_ownership_and_active_reference(self):
        root, home, workspace, previous = self.temp_context()
        try:
            core.configure_runtime_home(str(home))
            monitor = {
                "id": "monitor-owned",
                "sessionId": "session-owner",
                "taskId": "task-owned",
                "createdAt": now_iso(),
                "updatedAt": now_iso(),
                "status": "active",
                "goal": "watch",
                "checkIntervalMs": 1000,
                "heartbeatIntervalMs": 2000,
            }
            run(core.save_monitor(monitor))
            owner = {"id": "session-owner", "metadata": {"monitorIds": []}}
            other = {"id": "session-other", "metadata": {"monitorIds": ["monitor-owned"]}}
            with self.assertRaises(core.TaskOwnershipError) as foreign:
                run(core.requireModelOwnedMonitor({"session": other}, "monitor-owned"))
            self.assertEqual(foreign.exception.code, "task.not_owned")
            with self.assertRaises(core.TaskOwnershipError) as missing:
                run(core.requireModelOwnedMonitor({"session": owner}, "monitor-owned"))
            self.assertEqual(missing.exception.code, "task.not_referenced")
            owner["metadata"]["monitorIds"].append("monitor-owned")
            self.assertEqual(
                run(core.requireModelOwnedMonitor({"session": owner}, "monitor-owned"))["id"],
                "monitor-owned",
            )
        finally:
            self.restore_home(root, previous)

    def test_birth_token_mismatch_cannot_signal_reused_pid(self):
        persisted = {
            "schemaVersion": 1,
            "pid": 4242,
            "processGroupId": 4242,
            "spawnedAt": "2026-07-16T00:00:00Z",
            "startToken": "test:old",
            "observedCommandDigest": "a" * 64,
            "expectedCommandDigest": "b" * 64,
            "platform": sys.platform,
        }
        observed = {
            "pid": 4242,
            "processGroupId": 4242,
            "startToken": "test:reused",
            "observedCommandDigest": "a" * 64,
            "platform": sys.platform,
        }
        getattr(process_identity_module, "__setProcessIdentityObserverForTests")(lambda _pid: observed)
        with mock.patch.object(process_identity_module.os, "killpg") as kill_group:
            with self.assertRaises(core.ProcessIdentityError) as error:
                run(core.signalOwnedProcess(persisted, signal.SIGTERM))
            self.assertEqual(error.exception.code, "process.identity_mismatch")
            kill_group.assert_not_called()

    def test_observed_process_identity_binds_argv_not_only_the_executable(self):
        children = [
            subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)", marker],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            for marker in ("first-command", "second-command")
        ]
        identities = []
        try:
            for child, marker in zip(children, ("first-command", "second-command")):
                identities.append(
                    run(
                        core.captureOwnedProcessIdentity(
                            pid=child.pid,
                            expectedArgv=[
                                sys.executable,
                                "-c",
                                "import time; time.sleep(30)",
                                marker,
                            ],
                        )
                    )
                )
            self.assertNotEqual(
                identities[0]["observedCommandDigest"],
                identities[1]["observedCommandDigest"],
            )
        finally:
            for index, child in enumerate(children):
                if child.poll() is None:
                    if index < len(identities):
                        run(core.signalOwnedProcess(identities[index], signal.SIGTERM))
                    else:
                        child.terminate()
            for child in children:
                if child.poll() is None:
                    child.wait(timeout=5)

    def test_record_and_meta_process_identities_must_match_by_value(self):
        identity = {
            "schemaVersion": 1,
            "pid": 4242,
            "processGroupId": 4242,
            "spawnedAt": "2026-07-16T00:00:00Z",
            "startToken": "test:stable",
            "observedCommandDigest": "a" * 64,
            "expectedCommandDigest": "b" * 64,
            "platform": sys.platform,
        }
        reordered = {key: identity[key] for key in reversed(tuple(identity))}
        self.assertEqual(
            core.processIdentityFromRecord({"processIdentity": identity}, {"processIdentity": reordered}),
            identity,
        )
        changed = copy.deepcopy(identity)
        changed["startToken"] = "test:other"
        with self.assertRaises(core.ProcessIdentityError) as error:
            core.processIdentityFromRecord({"processIdentity": identity}, {"processIdentity": changed})
        self.assertEqual(error.exception.code, "process.identity_mismatch")
