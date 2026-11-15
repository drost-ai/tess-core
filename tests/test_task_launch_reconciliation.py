from __future__ import annotations

from tests.support.tess_core_case import *


class TaskLaunchReconciliationTests(TessCoreTestCase):
    def test_interrupted_starting_reservation_fails_once_and_repairs_session_reference(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            run(core.resolve_config({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.create_empty_session(
                cwd=str(workspace),
                provider="openai",
                model="gpt-5.4",
                systemPrompt="test",
                permissionMode="bypassPermissions",
                sessionId="session-interrupted-launch",
            )
            run(core.save_session(session))
            task_id = "task-interrupted-launch"
            reservation = core.create_task_launch_reservation(
                taskId=task_id,
                sessionId=session["id"],
                kind="background_command",
                now="2026-01-01T00:00:00Z",
            )
            common = {
                "taskId": task_id,
                "sessionId": session["id"],
                "command": "sleep 30",
                "cwd": str(workspace),
                "startedAt": reservation["createdAt"],
                "updatedAt": reservation["createdAt"],
                "status": "starting",
                "outputFile": core.task_output_path(task_id),
                "detailStatus": "starting",
                "owner": "daemon",
                "launchReservation": reservation,
            }
            run(core.write_task_meta(task_id, {"type": "background_command", **common}))
            run(
                core.save_task_record(
                    {
                        "id": task_id,
                        "sessionId": session["id"],
                        "sourceId": task_id,
                        "kind": "background_command",
                        "status": "queued",
                        "title": "interrupted",
                        "summary": "starting",
                        "createdAt": reservation["createdAt"],
                        "updatedAt": reservation["createdAt"],
                        "startedAt": reservation["createdAt"],
                        "foregroundable": True,
                        "restorable": True,
                        "command": "sleep 30",
                        "cwd": str(workspace),
                        "outputFile": core.task_output_path(task_id),
                        "detailStatus": "starting",
                        "owner": "daemon",
                        "launchReservation": reservation,
                    }
                )
            )
            events = []
            supervisor = run(core.create_task_supervisor({"cwd": str(workspace), "onEvent": lambda event: events.append(event)}))
            record = run(supervisor["inspectTask"](task_id))
            meta = run(core.read_task_meta(task_id))
            persisted = run(core.load_session_record_shallow(session["id"]))
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["launchReservation"]["state"], "failed")
            self.assertEqual(record["metadata"]["launch_recovery_state"], "interrupted_before_attachment")
            self.assertEqual(meta["status"], "failed")
            self.assertTrue(meta["retryable"])
            self.assertEqual(persisted["metadata"]["taskIds"], [task_id])
            self.assertEqual(sum(event["event"] == "task.failed" for event in events), 1)

            run(supervisor["close"]())
            supervisor = run(core.create_task_supervisor({"cwd": str(workspace), "onEvent": lambda event: events.append(event)}))
            persisted = run(core.load_session_record_shallow(session["id"]))
            self.assertEqual(persisted["metadata"]["taskIds"], [task_id])
            self.assertEqual(sum(event["event"] == "task.failed" for event in events), 1)
        finally:
            if supervisor:
                run(supervisor["close"]())
            run(core.stop_task_supervisor_daemon({"instanceId": "launch-recon-instance"}))
            self.restore_home(root, previous)

    def test_interrupted_subagent_repairs_parent_and_child_session_links(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            run(core.resolve_config({"runtimeHome": str(home)}, str(workspace)))
            parent = core.create_empty_session(
                cwd=str(workspace), provider="openai", model="gpt-5.4",
                systemPrompt="parent", permissionMode="bypassPermissions",
                sessionId="parent-interrupted-subagent",
            )
            child = core.create_empty_session(
                cwd=str(workspace), provider="openai", model="gpt-5.4",
                systemPrompt="child", permissionMode="bypassPermissions",
                sessionId="child-interrupted-subagent",
            )
            run(core.save_session(parent))
            run(core.save_session(child))
            task_id = "task-interrupted-subagent"
            reservation = core.create_task_launch_reservation(
                taskId=task_id,
                sessionId=parent["id"],
                kind="subagent",
                now="2026-01-01T00:00:00Z",
            )
            runtime_contract = {"concurrencyGroup": "probe"}
            run(
                core.write_task_meta(
                    task_id,
                    {
                        "type": "subagent",
                        "taskId": task_id,
                        "agent_id": "agent-interrupted-subagent",
                        "sessionId": child["id"],
                        "parent_session_id": parent["id"],
                        "runtimeContract": runtime_contract,
                        "status": "starting",
                        "detailStatus": "starting",
                        "owner": "daemon",
                        "updatedAt": reservation["createdAt"],
                        "launchReservation": reservation,
                    },
                )
            )
            run(
                core.save_task_record(
                    {
                        "id": task_id,
                        "sourceId": "agent-interrupted-subagent",
                        "sessionId": child["id"],
                        "kind": "subagent",
                        "status": "queued",
                        "title": "interrupted subagent",
                        "summary": "starting",
                        "createdAt": reservation["createdAt"],
                        "updatedAt": reservation["createdAt"],
                        "detailStatus": "starting",
                        "owner": "daemon",
                        "launchReservation": reservation,
                    }
                )
            )

            supervisor = run(core.create_task_supervisor({"cwd": str(workspace)}))
            parent_record = run(core.load_session_record_shallow(parent["id"]))
            child_record = run(core.load_session_record_shallow(child["id"]))
            self.assertEqual(parent_record["metadata"]["taskIds"], [task_id])
            self.assertNotIn("subagentTaskId", parent_record["metadata"])
            self.assertEqual(child_record["metadata"]["subagentTaskId"], task_id)
            self.assertEqual(
                child_record["metadata"]["subagentRuntimeContract"],
                runtime_contract,
            )
        finally:
            if supervisor:
                run(supervisor["close"]())
            run(core.stop_task_supervisor_daemon({"instanceId": "launch-recon-instance"}))
            self.restore_home(root, previous)
