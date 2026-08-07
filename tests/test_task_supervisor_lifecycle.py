from __future__ import annotations

import signal

from tests.support.agent_core_case import *
from agent_core.daemon.security import DaemonSecurityError


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class TaskSupervisorLifecycleTests(AgentCoreTestCase):
    def test_background_runner_accepts_reference_positional_contract(self):
        root, home, workspace, previous = self.temp_context()
        try:
            task_id = "runner-positional"
            meta_path = workspace / "task-meta.json"
            output_path = workspace / "task-output.log"
            repo_root = Path(__file__).parents[1]
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PYTHONPATH"] = str(repo_root) if not env.get("PYTHONPATH") else f"{repo_root}{os.pathsep}{env['PYTHONPATH']}"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_core.tasks.runners.background",
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
            self.assertEqual(zero_tail["output"], "")
            negative_tail = run(supervisor["readTaskOutput"](task_id, -10))
            self.assertTrue(negative_tail["truncated"])
            self.assertEqual(negative_tail["output"], "")
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
            run(core.stopTaskSupervisorDaemon({"instanceId": "sup-life-instance"}))
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
            stopped = run(supervisor["stopTask"](task_id))
            self.assertEqual((stopped["status"], stopped["changed"]), ("stopped", True))
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
            self.assertEqual(record["summary"], "Task stopped by deliberate request.")
            stopped_events = [event for event in events if event["event"] == "task.stopped" and event["data"]["task_id"] == task_id]
            self.assertEqual(len(stopped_events), 1)
            self.assertEqual(stopped_events[0]["data"]["summary"], "Task stopped by deliberate request.")
            self.assertEqual(stopped_events[0]["data"]["transition_id"], stopped["transitionId"])
            self.assertNotIn("updated_at", stopped_events[0]["data"])
        finally:
            if supervisor:
                run(supervisor["close"]())
            run(core.stopTaskSupervisorDaemon({"instanceId": "sup-life-instance"}))
            self.restore_home(root, previous)

    def test_daemon_socket_path_falls_back_for_deep_runtime_home(self):
        root, home, workspace, previous = self.temp_context()
        try:
            deep_home = home / ("nested-" + "x" * 80) / ("more-" + "y" * 80)
            deep_home.mkdir(parents=True)
            core.configureRuntimeHome(str(deep_home))
            socket_path = core.daemonSocketPath("deep-instance")
            self.assertLessEqual(len(socket_path.encode("utf-8")), 100)
            self.assertEqual(socket_path, core.daemonSocketPath("deep-instance"))
            self.assertNotEqual(socket_path, core.daemonSocketPath("other-instance"))
            self.assertFalse(socket_path.startswith(str(deep_home)))
            self.assertEqual(Path(socket_path).parent, Path(tempfile.gettempdir()))
        finally:
            self.restore_home(root, previous)

    def test_daemon_event_cursor_reads_tail_and_rotated_log(self):
        root, home, workspace, previous = self.temp_context()
        try:
            run(core.ensureBaseLayout(str(workspace)))
            events_path = Path(core.daemonEventsPath("sup-life-instance"))
            events_path.parent.mkdir(parents=True, exist_ok=True)
            records = [{"cursor": index, "event": "daemon.test"} for index in range(1, 40)]
            events_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
            self.assertEqual(run(core.readLastDaemonEventCursor("sup-life-instance")), 39)

            with events_path.open("a", encoding="utf-8") as handle:
                handle.write('{"cursor": 999')
            self.assertEqual(run(core.readLastDaemonEventCursor("sup-life-instance")), 39)

            self.assertTrue(run(core.rotateDaemonEventsIfOversized("sup-life-instance", capBytes=1)))
            self.assertFalse(events_path.exists())
            self.assertTrue(Path(f"{events_path}.1").exists())
            self.assertEqual(run(core.readLastDaemonEventCursor("sup-life-instance")), 39)
        finally:
            self.restore_home(root, previous)

    def test_daemon_connection_rejects_purpose_confused_stale_socket(self):
        root, home, workspace, previous = self.temp_context()
        try:
            run(core.ensureBaseLayout(str(workspace)))
            socket_path = core.daemonSocketPath("sup-life-instance")
            Path(socket_path).parent.mkdir(parents=True, exist_ok=True)
            Path(socket_path).write_text("stale", encoding="utf-8")
            run(core.saveDaemonState("sup-life-instance", {"pid": 999999999, "socketPath": socket_path, "status": "running"}))
            with self.assertRaises(DaemonSecurityError) as raised:
                run(core.connectTaskSupervisorDaemonClient({"instanceId": "sup-life-instance"}))
            self.assertEqual(raised.exception.code, "daemon.security_unsafe")
            self.assertEqual(Path(socket_path).read_text(encoding="utf-8"), "stale")
            self.assertIsNotNone(run(core.loadDaemonState("sup-life-instance")))
        finally:
            self.restore_home(root, previous)

    def test_boot_sweep_kills_dead_epoch_orphan_and_finalizes_daemon_lost(self):
        root, home, workspace, previous = self.temp_context()
        proc = None
        try:
            run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            task_id = "task-recovery"
            output_file = core.taskOutputPath(task_id)
            Path(output_file).parent.mkdir(parents=True, exist_ok=True)
            Path(output_file).write_text("still running\n", encoding="utf-8")
            child_argv = [sys.executable, "-c", "import time; time.sleep(10)"]
            proc = subprocess.Popen(
                child_argv,
                cwd=str(workspace),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            process_identity = run(
                core.captureOwnedProcessIdentity(pid=proc.pid, expectedArgv=child_argv)
            )
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
                "processIdentity": process_identity,
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
                        "processIdentity": process_identity,
                        "metadata": {"pid": proc.pid},
                    }
                )
            )
            client = run(core.ensureTaskSupervisorDaemon({"instanceId": "sup-life-instance", "cwd": str(workspace)}))
            record = None
            for _ in range(100):
                record = run(client["inspectTask"](task_id))
                if record and record.get("detailStatus") == "daemon_lost":
                    break
                time.sleep(0.05)
            self.assertIsNotNone(record)
            self.assertEqual(record["status"], "stopped")
            self.assertEqual(record["detailStatus"], "daemon_lost")
            self.assertIn("process_kill_outcome", record["metadata"])
            next_meta = run(core.readTaskMeta(task_id))
            self.assertEqual(next_meta["status"], "terminated")
            deadline = time.monotonic() + 5
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIsNotNone(proc.poll(), "orphaned worker must be killed by the boot sweep")
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            run(core.stopTaskSupervisorDaemon({"instanceId": "sup-life-instance"}))
            self.restore_home(root, previous)

    def test_termination_ladder_escalates_to_kill_for_term_immune_children(self):
        from agent_core.process._termination import terminate_popen_group

        polite = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        self.assertEqual(run(terminate_popen_group(polite, term_grace_ms=2_000, kill_grace_ms=1_000)), "terminated")
        self.assertIsNotNone(polite.poll())

        stubborn = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('armed', flush=True)\ntime.sleep(60)",
            ],
            start_new_session=True,
            stdout=subprocess.PIPE,
        )
        self.assertEqual(stubborn.stdout.readline().strip(), b"armed")
        self.assertEqual(run(terminate_popen_group(stubborn, term_grace_ms=400, kill_grace_ms=2_000)), "killed")
        self.assertIsNotNone(stubborn.poll())

    def test_supervisor_close_reports_then_kills_wedged_worker(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            run(core.saveSession(session))
            events = []

            async def on_event(event):
                events.append(event)

            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace), "onEvent": on_event}))
            script = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('armed', flush=True)\ntime.sleep(120)"
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
            started = run(supervisor["startBackgroundCommand"]({"sessionId": session["id"], "command": command, "cwd": str(workspace), "title": "wedged worker"}))
            task_id = started["taskId"]
            output_file = core.taskOutputPath(task_id)
            for _ in range(100):
                if Path(output_file).exists() and "armed" in Path(output_file).read_text(encoding="utf-8"):
                    break
                time.sleep(0.05)
            from agent_core.tasks.supervisor_parts.shared import _PROCESS_REGISTRY

            proc = _PROCESS_REGISTRY.get(task_id)
            self.assertIsNotNone(proc)
            os.environ["AGENT_CORE_PROCESS_TERM_GRACE_MS"] = "400"
            try:
                run(supervisor["close"]())
            finally:
                del os.environ["AGENT_CORE_PROCESS_TERM_GRACE_MS"]
            self.assertIsNotNone(proc.poll())
            stopping = [event for event in events if event.get("event") == "supervisor.stopping_workers"]
            self.assertEqual(len(stopping), 1)
            self.assertEqual(stopping[0]["data"]["count"], 1)
            self.assertIn(task_id, stopping[0]["data"]["task_ids"])
        finally:
            self.restore_home(root, previous)

    def test_bootstrap_starts_daemon_default_on_and_close_stops_it(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            run(core.saveSession(session))
            boot = run(core.bootstrapRuntime({"session": session, "config": config, "cwd": str(workspace)}))
            instance_id = boot["runtimeInstanceId"]
            try:
                self.assertIsNotNone(boot.get("daemon"))
                state = run(core.loadDaemonState(instance_id))
                self.assertIsNotNone(state)
                daemon_pid = state["pid"]
                self.assertTrue(_pid_alive(daemon_pid))
            finally:
                run(boot["close"]())
            self.assertIsNone(run(core.loadDaemonState(instance_id)))
            deadline = time.monotonic() + 5
            while _pid_alive(daemon_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(_pid_alive(daemon_pid))
        finally:
            self.restore_home(root, previous)

    def test_daemon_dies_with_owner_and_kills_owned_workers(self):
        root, home, workspace, previous = self.temp_context()
        owner = None
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            run(core.saveSession(session))
            owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
            os.environ["AGENT_CORE_DAEMON_OWNER_POLL_MS"] = "200"
            client = run(core.ensureTaskSupervisorDaemon({"instanceId": "owner-watch-instance", "cwd": str(workspace), "ownerPid": owner.pid}))
            daemon_pid = run(core.loadDaemonState("owner-watch-instance"))["pid"]
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(120)')}"
            started = run(client["startBackgroundCommand"]({"sessionId": session["id"], "command": command, "cwd": str(workspace), "title": "contained worker"}))
            task_id = started["taskId"]
            worker_pid = None
            for _ in range(100):
                meta = run(core.readTaskMeta(task_id))
                if meta and isinstance(meta.get("pid"), int):
                    worker_pid = meta["pid"]
                    break
                time.sleep(0.05)
            self.assertIsNotNone(worker_pid)
            run(client["close"]())

            owner.kill()
            owner.wait(timeout=5)
            from agent_core.daemon.lifecycle import _DAEMON_PROCESSES

            spawned = _DAEMON_PROCESSES.get("owner-watch-instance")
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                daemon_gone = spawned.poll() is not None if spawned is not None else not _pid_alive(daemon_pid)
                if daemon_gone and not _pid_alive(worker_pid):
                    break
                time.sleep(0.1)
            self.assertTrue(spawned.poll() is not None if spawned is not None else not _pid_alive(daemon_pid), "daemon must die with its owner")
            self.assertFalse(_pid_alive(worker_pid), "owned workers must die with the daemon")
            events = run(core.loadDaemonEvents("owner-watch-instance"))
            self.assertTrue(any(event.get("event") == "daemon.owner_lost" for event in events))
            self.assertIsNone(run(core.loadDaemonState("owner-watch-instance")))
        finally:
            os.environ.pop("AGENT_CORE_DAEMON_OWNER_POLL_MS", None)
            if owner and owner.poll() is None:
                owner.kill()
            run(core.stopTaskSupervisorDaemon({"instanceId": "owner-watch-instance"}))
            self.restore_home(root, previous)

    def test_runtime_watchdog_replaces_killed_daemon_with_one_fresh_instance(self):
        from agent_core.runtime._daemon_watch import check_daemon_once

        root, home, workspace, previous = self.temp_context()
        try:
            client = run(core.ensureTaskSupervisorDaemon({"instanceId": "watchdog-instance", "cwd": str(workspace)}))
            old_state = run(core.loadDaemonState("watchdog-instance"))
            run(client["close"]())
            emitted = []
            swapped = []
            context = {
                "instanceId": "watchdog-instance",
                "cwd": str(workspace),
                "emit": lambda event, data: emitted.append((event, data)),
                "setDaemon": lambda fresh: swapped.append(fresh),
            }
            self.assertEqual(run(check_daemon_once(context)), "healthy")
            self.assertEqual(emitted, [])

            os.kill(old_state["pid"], signal.SIGKILL)
            from agent_core.daemon.lifecycle import _DAEMON_PROCESSES

            spawned = _DAEMON_PROCESSES.get("watchdog-instance")
            deadline = time.monotonic() + 5
            while spawned is not None and spawned.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(run(check_daemon_once(context)), "restarted")
            new_state = run(core.loadDaemonState("watchdog-instance"))
            self.assertNotEqual(new_state["pid"], old_state["pid"])
            self.assertNotEqual(new_state["epoch"], old_state["epoch"])
            self.assertEqual([event for event, _data in emitted], ["daemon.lost", "daemon.restarted"])
            self.assertEqual(len(swapped), 1)
            run(swapped[0]["close"]())
        finally:
            run(core.stopTaskSupervisorDaemon({"instanceId": "watchdog-instance"}))
            self.restore_home(root, previous)

    def test_two_runtime_instances_coexist_with_disjoint_daemons(self):
        root, home, workspace, previous = self.temp_context()
        try:
            first = run(core.ensureTaskSupervisorDaemon({"instanceId": "coexist-a", "cwd": str(workspace)}))
            second = run(core.ensureTaskSupervisorDaemon({"instanceId": "coexist-b", "cwd": str(workspace)}))
            state_a = run(core.loadDaemonState("coexist-a"))
            state_b = run(core.loadDaemonState("coexist-b"))
            self.assertEqual(state_a["instanceId"], "coexist-a")
            self.assertEqual(state_b["instanceId"], "coexist-b")
            self.assertNotEqual(state_a["pid"], state_b["pid"])
            self.assertNotEqual(state_a["socketPath"], state_b["socketPath"])
            run(first["close"]())
            run(second["close"]())

            run(core.stopTaskSupervisorDaemon({"instanceId": "coexist-a"}))
            self.assertIsNone(run(core.loadDaemonState("coexist-a")))
            surviving = run(core.loadDaemonState("coexist-b"))
            self.assertIsNotNone(surviving)
            self.assertEqual(surviving["pid"], state_b["pid"])
            client_b = run(core.connectTaskSupervisorDaemonClient({"instanceId": "coexist-b"}))
            self.assertIsNotNone(client_b)
            run(client_b["close"]())
        finally:
            run(core.stopTaskSupervisorDaemon({"instanceId": "coexist-a"}))
            run(core.stopTaskSupervisorDaemon({"instanceId": "coexist-b"}))
            self.restore_home(root, previous)
