from __future__ import annotations

from tests.support.agent_core_case import *


class TaskSupervisorLifecycleTests(AgentCoreTestCase):
    def test_background_runner_accepts_reference_positional_contract(self):
        root, home, workspace, previous = self.temp_context()
        try:
            task_id = "runner-positional"
            meta_path = workspace / "task-meta.json"
            output_path = workspace / "task-output.log"
            repo_root = Path(__file__).parents[1]
            env = os.environ.copy()
            env["PYTHONPATH"] = str(repo_root) if not env.get("PYTHONPATH") else f"{repo_root}{os.pathsep}{env['PYTHONPATH']}"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_core.background_task_runner",
                    task_id,
                    str(meta_path),
                    str(output_path),
                    str(workspace),
                    os.environ.get("SHELL") or "/bin/sh",
                    "printf runner-parity",
                ],
                cwd=str(workspace),
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output_path.read_text(encoding="utf-8"), "runner-parity")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta["taskId"], task_id)
            self.assertEqual(meta["command"], "printf runner-parity")
            self.assertEqual(meta["cwd"], str(workspace))
            self.assertEqual(meta["outputFile"], str(output_path))
            self.assertEqual(meta["status"], "completed")
            self.assertEqual(meta["exitCode"], 0)
            self.assertIsInstance(meta["pid"], int)
            self.assertIsInstance(meta["runnerPid"], int)
        finally:
            self.restore_home(root, previous)

    def test_task_supervisor_read_output_defaults_to_reference_tail_size(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            task_id = "tail-default-task"
            output_file = core.taskOutputPath(task_id)
            Path(output_file).parent.mkdir(parents=True, exist_ok=True)
            Path(output_file).write_text("a" * 13000, encoding="utf-8")
            run(core.writeTaskMeta(task_id, {"type": "background_command", "taskId": task_id, "id": task_id, "sessionId": "tail-session", "command": "long-output", "cwd": str(workspace), "startedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z", "status": "completed", "outputFile": output_file}))
            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace)}))
            default_tail = run(supervisor["readTaskOutput"](task_id))
            self.assertTrue(default_tail["truncated"])
            self.assertEqual(len(default_tail["output"]), 12000)
            public_tail = run(core.readTaskOutput(task_id, tail_bytes=64))
            self.assertTrue(public_tail["truncated"])
            self.assertEqual(len(public_tail["output"]), 64)
            zero_tail = run(supervisor["readTaskOutput"](task_id, 0))
            self.assertTrue(zero_tail["truncated"])
            self.assertEqual(zero_tail["output"], "a")
            negative_tail = run(supervisor["readTaskOutput"](task_id, -10))
            self.assertTrue(negative_tail["truncated"])
            self.assertEqual(negative_tail["output"], "a")
        finally:
            if supervisor:
                run(supervisor["close"]())
            self.restore_home(root, previous)

    def test_task_supervisor_launch_records_backgrounded_task_and_session_metadata(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            run(core.saveSession(session))
            events = []
            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace), "onEvent": lambda event: events.append(event)}))
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(0.4)')}"
            started = run(supervisor["startBackgroundCommand"]({"sessionId": session["id"], "command": command, "cwd": str(workspace), "title": "background parity"}))
            task_id = started["taskId"]
            record = run(supervisor["inspectTask"](task_id))
            self.assertEqual(record["status"], "backgrounded")
            self.assertEqual(record["summary"], "Background command launched by daemon.")
            self.assertEqual(record["detailStatus"], "launching")
            self.assertEqual(record["taskFamily"], "shell")
            self.assertTrue(record["retrieveOnly"])
            persisted = run(core.loadSession(session["id"]))
            self.assertIn(task_id, persisted["metadata"]["taskIds"])
            start_event = next(event for event in events if event["event"] == "task.updated" and event["data"]["task_id"] == task_id)
            self.assertEqual(start_event["data"]["status"], "backgrounded")
            self.assertEqual(start_event["data"]["summary"], "Background command launched by daemon.")
            self.assertEqual(start_event["data"]["detail_status"], "launching")
            self.assertNotIn("updated_at", start_event["data"])
        finally:
            if supervisor:
                run(supervisor["close"]())
            run(core.stopTaskSupervisorDaemon())
            self.restore_home(root, previous)

    def test_task_supervisor_stop_process_finalizes_through_exit_watcher(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            run(core.saveSession(session))
            events = []
            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace), "onEvent": lambda event: events.append(event)}))
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(10)')}"
            started = run(supervisor["startBackgroundCommand"]({"sessionId": session["id"], "command": command, "cwd": str(workspace), "title": "stop parity"}))
            task_id = started["taskId"]
            self.assertTrue(run(supervisor["stopTask"](task_id)))
            for _ in range(60):
                meta = run(core.readTaskMeta(task_id))
                if meta and meta.get("status") == "terminated":
                    break
                time.sleep(0.05)
            meta = run(core.readTaskMeta(task_id))
            self.assertEqual(meta["status"], "terminated")
            self.assertIsNone(meta.get("exitCode"))
            record = run(supervisor["inspectTask"](task_id))
            self.assertEqual(record["status"], "stopped")
            self.assertEqual(record["summary"], "Task was terminated.")
            failed_events = [event for event in events if event["event"] == "task.failed" and event["data"]["task_id"] == task_id]
            self.assertEqual(len(failed_events), 1)
            self.assertEqual(failed_events[0]["data"]["summary"], "Task was terminated.")
            self.assertNotIn("updated_at", failed_events[0]["data"])
        finally:
            if supervisor:
                run(supervisor["close"]())
            run(core.stopTaskSupervisorDaemon())
            self.restore_home(root, previous)

    def test_daemon_connection_cleans_stale_state_and_socket(self):
        root, home, workspace, previous = self.temp_context()
        try:
            run(core.ensureBaseLayout(str(workspace)))
            socket_path = core.daemonSocketPath()
            Path(socket_path).parent.mkdir(parents=True, exist_ok=True)
            Path(socket_path).write_text("stale", encoding="utf-8")
            run(core.saveDaemonState({"pid": 999999999, "socketPath": socket_path, "status": "running"}))
            self.assertIsNone(run(core.connectTaskSupervisorDaemonClient()))
            self.assertFalse(Path(socket_path).exists())
            self.assertIsNone(run(core.loadDaemonState()))
        finally:
            self.restore_home(root, previous)

    def test_daemon_reconciles_persisted_running_task_after_restart(self):
        root, home, workspace, previous = self.temp_context()
        proc = None
        try:
            run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            task_id = "task-recovery"
            output_file = core.taskOutputPath(task_id)
            Path(output_file).parent.mkdir(parents=True, exist_ok=True)
            Path(output_file).write_text("still running\n", encoding="utf-8")
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], cwd=str(workspace), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            created_at = "2026-01-01T00:00:00Z"
            meta = {
                "type": "background_command",
                "taskId": task_id,
                "id": task_id,
                "sessionId": "session-recovery",
                "command": "sleep",
                "cwd": str(workspace),
                "startedAt": created_at,
                "updatedAt": created_at,
                "status": "running",
                "outputFile": output_file,
                "pid": proc.pid,
                "detailStatus": "running",
                "owner": "daemon",
                "supervisorId": "previous-supervisor",
            }
            run(core.writeTaskMeta(task_id, meta))
            run(
                core.saveTaskRecord(
                    {
                        "id": task_id,
                        "sessionId": "session-recovery",
                        "sourceId": task_id,
                        "kind": "background_command",
                        "status": "running",
                        "title": "sleep",
                        "summary": "Background command is running.",
                        "createdAt": created_at,
                        "updatedAt": created_at,
                        "startedAt": created_at,
                        "cwd": str(workspace),
                        "command": "sleep",
                        "outputFile": output_file,
                        "outputCursor": len("still running\n"),
                        "foregroundMode": "background",
                        "taskFamily": "shell",
                        "detailStatus": "running",
                        "retrieveOnly": True,
                        "owner": "daemon",
                        "supervisorId": "previous-supervisor",
                        "metadata": {"pid": proc.pid},
                    }
                )
            )
            client = run(core.ensureTaskSupervisorDaemon({"cwd": str(workspace)}))
            record = None
            for _ in range(40):
                record = run(client["inspectTask"](task_id))
                if record and record.get("detailStatus") == "recovered_after_daemon_restart":
                    break
                time.sleep(0.05)
            self.assertIsNotNone(record)
            self.assertEqual(record["status"], "degraded")
            self.assertEqual(record["detailStatus"], "recovered_after_daemon_restart")
            self.assertEqual(record["metadata"]["recovery_state"], "recovered_after_daemon_restart")
            next_meta = run(core.readTaskMeta(task_id))
            self.assertEqual(next_meta["status"], "reconciling")
            self.assertEqual(next_meta["detailStatus"], "running")
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            run(core.stopTaskSupervisorDaemon())
            self.restore_home(root, previous)
