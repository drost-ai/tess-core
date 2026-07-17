from __future__ import annotations

from tests.support.agent_core_case import *


class TaskSupervisorCoreTests(AgentCoreTestCase):
    def test_background_task_supervisor_and_task_output(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            bash = next(tool for tool in core.createBuiltinTools() if tool.name == "Bash")
            base_context = {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None}
            foreground = run(bash.handler({"command": "printf foreground-ok"}, base_context))
            foreground_payload = json.loads(foreground["content"])
            self.assertEqual(foreground_payload["exitCode"], 0)
            self.assertEqual(foreground["meta"]["execution"]["kind"], "tool")
            self.assertEqual(foreground["meta"]["execution"]["status"], "completed")
            self.assertEqual(foreground["meta"]["execution"]["metadata"]["exit_code"], 0)

            fake_shell = workspace / "fake-shell.py"
            fake_shell_args = workspace / "fake-shell-args.txt"
            fake_shell.write_text(
                textwrap.dedent(
                    """
                    #!/usr/bin/env python3
                    import os
                    import pathlib
                    import sys

                    pathlib.Path(os.environ["FAKE_SHELL_ARGS"]).write_text("\\n".join(sys.argv[1:]), encoding="utf-8")
                    print("fake-shell:" + (sys.argv[2] if len(sys.argv) > 2 else ""))
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            fake_shell.chmod(0o755)
            previous_shell = os.environ.get("SHELL")
            previous_fake_shell_args = os.environ.get("FAKE_SHELL_ARGS")
            try:
                os.environ["SHELL"] = str(fake_shell)
                os.environ["FAKE_SHELL_ARGS"] = str(fake_shell_args)
                shell_result = run(bash.handler({"command": "printf shell-parity"}, base_context))
            finally:
                if previous_shell is None:
                    os.environ.pop("SHELL", None)
                else:
                    os.environ["SHELL"] = previous_shell
                if previous_fake_shell_args is None:
                    os.environ.pop("FAKE_SHELL_ARGS", None)
                else:
                    os.environ["FAKE_SHELL_ARGS"] = previous_fake_shell_args
            shell_payload = json.loads(shell_result["content"])
            self.assertEqual(shell_payload["stdout"], "fake-shell:printf shell-parity\n")
            self.assertEqual(fake_shell_args.read_text(encoding="utf-8"), "-lc\nprintf shell-parity")

            timeout_command = f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(1)')}"
            timed = run(bash.handler({"command": timeout_command, "timeout": 0.05}, base_context))
            timed_payload = json.loads(timed["content"])
            self.assertTrue(timed["isError"])
            self.assertTrue(timed_payload["timedOut"])
            self.assertIsNone(timed_payload["exitCode"])
            self.assertEqual(timed["meta"]["execution"]["status"], "failed")
            self.assertEqual(timed["meta"]["execution"]["metadata"], {"exit_code": None, "timed_out": True})

            abort_events = []
            with self.assertRaises(RuntimeError) as abort_ctx:
                run(bash.handler({"command": "printf should-not-run"}, {**base_context, "abortSignal": {"aborted": True}, "outputJson": lambda event, data: abort_events.append((event, data))}))
            self.assertEqual(getattr(abort_ctx.exception, "name", None), "AbortError")
            self.assertEqual(str(abort_ctx.exception), "Tool execution cancelled")
            self.assertEqual(abort_events, [])

            script = 'print("hello from background")'
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
            events = []
            result = run(bash.handler({"command": command, "run_in_background": True}, {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda event, data: events.append((event, data)), "cwd": str(workspace), "persistSession": lambda: None}))
            start_payload = json.loads(result["content"])
            task_id = start_payload["task_id"]
            self.assertEqual(start_payload["execution"]["kind"], "task")
            self.assertEqual(start_payload["execution"]["id"], task_id)
            self.assertEqual(result["meta"]["execution"]["activities"][0]["subjectId"], task_id)
            self.assertIn(task_id, session["metadata"]["taskIds"])
            self.assertEqual(events[-1][0], "task.registered")
            self.assertEqual(events[-1][1]["owner"], "client")
            self.assertEqual(events[-1][1]["task_id"], task_id)
            self.assertIsNone(run(core.getTaskSupervisorDaemonState()))
            for _ in range(80):
                meta = run(core.readTaskMeta(task_id))
                if meta and meta.get("status") == "completed":
                    break
                time.sleep(0.05)
            meta = run(core.readTaskMeta(task_id))
            self.assertEqual(meta["status"], "completed")
            self.assertEqual(meta["owner"], "client")
            self.assertIsInstance(meta["runnerPid"], int)
            self.assertIsInstance(meta["pid"], int)
            task_output = next(tool for tool in core.createBuiltinTools() if tool.name == "TaskOutput")
            output = run(task_output.handler({"task_id": task_id, "block": True, "timeout": 1000}, {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace)}))
            payload = json.loads(output["content"])
            self.assertIn("hello from background", payload["output"])
            self.assertEqual(payload["execution"]["id"], task_id)
            self.assertGreater(payload["execution"]["output"]["bytes"], 0)
            self.assertEqual(output["meta"]["execution"]["metadata"]["output_file"], payload["outputFile"])
            self.assertNotIn("output_file", payload)
            self.assertNotIn("task_id", payload)

            daemon_task_id = "task-output-daemon"
            session["metadata"]["taskIds"].append(daemon_task_id)
            daemon_output = core.taskOutputPath(daemon_task_id)
            Path(daemon_output).parent.mkdir(parents=True, exist_ok=True)
            Path(daemon_output).write_text("file output\n", encoding="utf-8")
            run(core.writeTaskMeta(daemon_task_id, {"taskId": daemon_task_id, "id": daemon_task_id, "sessionId": session["id"], "command": "daemon-output", "cwd": str(workspace), "startedAt": "2026-01-01T00:00:00Z", "status": "completed", "outputFile": daemon_output}))

            async def read_daemon_output(_task_id):
                return {"output": "daemon output\n", "truncated": True}

            daemon_result = run(task_output.handler({"task_id": daemon_task_id, "block": False, "timeout": 1000}, {**base_context, "daemon": {"readTaskOutput": read_daemon_output}}))
            daemon_payload = json.loads(daemon_result["content"])
            self.assertEqual(daemon_payload["output"], "daemon output\n")
            self.assertTrue(daemon_payload["output_truncated"])

            supervisor_only = run(task_output.handler({"task_id": daemon_task_id, "block": False, "timeout": 1000}, {**base_context, "taskSupervisor": {"readTaskOutput": read_daemon_output}}))
            supervisor_payload = json.loads(supervisor_only["content"])
            self.assertEqual(supervisor_payload["output"], "file output\n")
            self.assertFalse(supervisor_payload["output_truncated"])

            with self.assertRaises(RuntimeError) as output_abort_ctx:
                run(task_output.handler({"task_id": daemon_task_id, "block": True, "timeout": 1000}, {**base_context, "abortSignal": {"aborted": True}}))
            self.assertEqual(getattr(output_abort_ctx.exception, "name", None), "AbortError")
        finally:
            run(core.stopTaskSupervisorDaemon())
            self.restore_home(root, previous)

    def test_daemon_live_subscription_receives_task_events(self):
        root, home, workspace, previous = self.temp_context()
        unsubscribe = None
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            run(core.saveSession(session))
            client = run(core.ensureTaskSupervisorDaemon({"cwd": str(workspace)}))
            events = []
            unsubscribe = client["subscribe"](lambda event: events.append(event), session["id"])
            script = 'import time; print("subscription event"); time.sleep(0.2)'
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
            started = run(client["startBackgroundCommand"]({"sessionId": session["id"], "command": command, "cwd": str(workspace)}))
            task_id = started["taskId"]
            for _ in range(80):
                if any(event.get("event") == "task.completed" and event.get("data", {}).get("task_id") == task_id for event in events):
                    break
                time.sleep(0.05)
            task_events = [event for event in events if event.get("data", {}).get("task_id") == task_id]
            self.assertTrue(any(event.get("event") == "task.updated" for event in task_events))
            self.assertTrue(any(event.get("event") == "task.completed" for event in task_events))
            self.assertTrue(all(event.get("sessionId") == session["id"] for event in task_events))
            completed = next(event for event in task_events if event.get("event") == "task.completed")
            self.assertEqual(completed["data"]["task_family"], "shell")
            self.assertEqual(completed["data"]["detail_status"], "result_ready")
            self.assertEqual(completed["data"]["foreground_mode"], "background")
            self.assertTrue(completed["data"]["retrieve_only"])
            self.assertIn("result_summary", completed["data"])
        finally:
            if unsubscribe:
                unsubscribe()
            run(core.stopTaskSupervisorDaemon())
            self.restore_home(root, previous)

    def test_daemon_bootstrap_snapshots_attachment_counts_and_event_replay(self):
        root, home, workspace, previous = self.temp_context()
        unsubscribe = None
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            run(core.saveSession(session))
            client = run(core.ensureTaskSupervisorDaemon({"cwd": str(workspace)}))
            script = 'print("bootstrap replay")'
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
            started = run(client["startBackgroundCommand"]({"sessionId": session["id"], "command": command, "cwd": str(workspace), "title": "bootstrap replay"}))
            task_id = started["taskId"]
            for _ in range(60):
                meta = run(core.readTaskMeta(task_id))
                if meta and meta.get("status") == "completed":
                    break
                time.sleep(0.05)
            monitor = core.createMonitorRecord({"sessionId": session["id"], "taskId": task_id, "goal": "watch replay"})
            run(core.saveMonitor(monitor))

            bootstrap = run(client["getBootstrap"](session["id"]))
            self.assertGreaterEqual(bootstrap["taskSnapshot"]["total"], 1)
            self.assertTrue(any(task.get("id") == task_id for task in bootstrap["taskSnapshot"]["tasks"]))
            self.assertEqual(bootstrap["monitorSnapshot"]["total"], 1)
            self.assertEqual(len(bootstrap["collaborationSnapshot"]["workers"]), 1)
            self.assertEqual(bootstrap["collaborationSnapshot"]["workers"][0]["kind"], "monitor")
            self.assertEqual(bootstrap["collaborationSnapshot"]["workers"][0]["monitorId"], monitor["id"])

            persisted_events = run(core.loadDaemonEvents())
            self.assertTrue(all(isinstance(event.get("cursor"), int) for event in persisted_events))
            self.assertEqual([event["cursor"] for event in persisted_events], sorted(event["cursor"] for event in persisted_events))

            events = []
            unsubscribe = client["subscribe"](events.append, {"sessionId": session["id"], "afterCursor": 0})
            for _ in range(60):
                if any(event.get("event") == "task.snapshot" for event in events) and any(event.get("event") == "task.completed" and event.get("taskId") == task_id for event in events):
                    break
                time.sleep(0.05)
            self.assertTrue(any(event.get("event") == "task.snapshot" for event in events))
            self.assertTrue(any(event.get("event") == "monitor.snapshot" for event in events))
            self.assertTrue(any(event.get("event") == "task.completed" and event.get("taskId") == task_id for event in events))
            for _ in range(40):
                state = run(core.getTaskSupervisorDaemonState())
                if state and state.get("attachedClients") == 1:
                    break
                time.sleep(0.05)
            self.assertEqual(run(core.getTaskSupervisorDaemonState())["attachedClients"], 1)
            unsubscribe()
            unsubscribe = None
            for _ in range(40):
                state = run(core.getTaskSupervisorDaemonState())
                if state and state.get("attachedClients") == 0:
                    break
                time.sleep(0.05)
            self.assertEqual(run(core.getTaskSupervisorDaemonState())["attachedClients"], 0)
        finally:
            if unsubscribe:
                unsubscribe()
            run(core.stopTaskSupervisorDaemon())
            self.restore_home(root, previous)

    def test_task_supervisor_bootstrap_projects_long_lived_task_families(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            session["messages"].append({"role": "user", "content": "Coordinate the long lived work."})
            monitor = core.createMonitorRecord({"sessionId": session["id"], "taskId": "bootstrap-monitor-task", "goal": "watch bootstrap families"})
            session["metadata"]["monitorIds"] = [monitor["id"]]
            session["metadata"]["childAgentIds"] = ["bootstrap-agent"]
            run(core.saveSession(session))
            run(core.saveMonitor(monitor))
            run(
                core.saveChildAgent(
                    {
                        "agentId": "bootstrap-agent",
                        "sessionId": "bootstrap-child-session",
                        "parentSessionId": session["id"],
                        "taskId": "bootstrap-child-task",
                        "name": "Bootstrap child",
                        "provider": "openai",
                        "model": "gpt-5.4",
                        "mode": "bypassPermissions",
                        "runInBackground": True,
                        "status": "backgrounded",
                        "startedAt": "2026-01-01T00:00:00Z",
                        "lastUpdatedAt": "2026-01-01T00:00:02Z",
                        "lastSummary": "Working from child session",
                        "attachableSessionId": "bootstrap-child-session",
                        "owner": "daemon",
                        "retryable": False,
                        "stoppable": True,
                    }
                )
            )
            remote = run(core.createRemoteSession({"session": session, "description": "Bootstrap remote", "channelServers": ["local"]}))
            remote = run(core.updateRemoteSession(remote["id"], {"status": "awaiting_input", "lastEventSummary": "Remote wants input"}))
            session["metadata"]["remoteSessionId"] = remote["id"]
            run(core.saveSession(session))

            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace)}))
            bootstrap = run(supervisor["getBootstrap"](session["id"]))
            tasks = {task["id"]: task for task in bootstrap["taskSnapshot"]["tasks"]}
            self.assertEqual(tasks[f"monitor:{monitor['id']}"]["kind"], "monitor")
            self.assertEqual(tasks[f"monitor:{monitor['id']}"]["taskFamily"], "monitor")
            self.assertEqual(tasks["bootstrap-child-task"]["kind"], "subagent")
            self.assertEqual(tasks["bootstrap-child-task"]["status"], "backgrounded")
            self.assertEqual(tasks["bootstrap-child-task"]["metadata"]["agent_id"], "bootstrap-agent")
            self.assertEqual(tasks[f"remote:{remote['id']}"]["kind"], "remote")
            self.assertEqual(tasks[f"remote:{remote['id']}"]["status"], "awaiting_input")
            self.assertEqual(bootstrap["taskSnapshot"]["awaitingInput"], 1)
            self.assertGreaterEqual(bootstrap["taskSnapshot"]["active"], 3)
            restored = run(core.loadSession(session["id"]))["metadata"]["runtimeRestore"]
            restored_ids = {task["id"] for task in restored["activeTasks"]}
            self.assertIn(f"monitor:{monitor['id']}", restored_ids)
            self.assertIn("bootstrap-child-task", restored_ids)
            self.assertIn(f"remote:{remote['id']}", restored_ids)
            self.assertEqual(restored["restoredTaskCount"], bootstrap["taskSnapshot"]["total"])
            collaboration = bootstrap["collaborationSnapshot"]
            self.assertEqual(len(collaboration["workers"]), 3)
            self.assertEqual(collaboration["activeCount"], 2)
            self.assertEqual(collaboration["awaitingInputCount"], 1)
            self.assertEqual(collaboration["backgroundCount"], 2)
            self.assertEqual(collaboration["remoteStatus"], "awaiting_input")
        finally:
            if supervisor:
                run(supervisor["close"]())
            self.restore_home(root, previous)

    def test_task_runtime_emits_restored_registered_output_delta_and_restore_metadata(self):
        root, home, workspace, previous = self.temp_context()
        runtime = None
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            task_id = "runtime-task-family"
            output_file = core.taskOutputPath(task_id)
            Path(output_file).parent.mkdir(parents=True, exist_ok=True)
            Path(output_file).write_text("first\n", encoding="utf-8")
            session["metadata"]["taskIds"] = [task_id]
            run(core.saveSession(session))
            run(
                core.writeTaskMeta(
                    task_id,
                    {
                        "type": "background_command",
                        "taskId": task_id,
                        "id": task_id,
                        "sessionId": session["id"],
                        "command": "pytest",
                        "cwd": str(workspace),
                        "startedAt": "2026-01-01T00:00:00Z",
                        "updatedAt": "2026-01-01T00:00:00Z",
                        "status": "running",
                        "outputFile": output_file,
                        "title": "Runtime family",
                        "detailStatus": "running",
                        "owner": "daemon",
                    },
                )
            )
            events = []
            runtime = run(core.createTaskRuntime({"session": session, "config": config, "emit": lambda event, data: events.append((event, data)), "tickIntervalMs": 50}))
            task_events = [event for event, data in events if data.get("task_id") == task_id]
            self.assertEqual(task_events[:2], ["task.restored", "task.registered"])
            self.assertTrue(any(event == "task.snapshot" and data["tasks"][0]["task_family"] == "shell" for event, data in events))
            restored = run(core.loadSession(session["id"]))["metadata"]["runtimeRestore"]
            self.assertEqual(restored["activeTasks"][0]["id"], task_id)
            self.assertEqual(restored["restoredTaskCount"], 1)

            Path(output_file).write_text("first\nsecond\n", encoding="utf-8")
            for _ in range(40):
                if any(event == "task.output_delta" and data.get("task_id") == task_id for event, data in events):
                    break
                time.sleep(0.05)
            delta = next(data for event, data in events if event == "task.output_delta" and data.get("task_id") == task_id)
            self.assertGreater(delta["bytes_delta"], 0)

            meta = run(core.readTaskMeta(task_id))
            meta.update({"status": "completed", "finishedAt": "2026-01-01T00:00:03Z", "updatedAt": "2026-01-01T00:00:03Z"})
            run(core.writeTaskMeta(task_id, meta))
            for _ in range(40):
                if any(event == "task.completed" and data.get("task_id") == task_id for event, data in events):
                    break
                time.sleep(0.05)
            self.assertTrue(any(event == "task.completed" and data.get("task_id") == task_id for event, data in events))
        finally:
            if runtime:
                run(runtime["close"]())
            self.restore_home(root, previous)

    def test_task_runtime_projects_live_session_and_ignores_stale_records(self):
        root, home, workspace, previous = self.temp_context()
        runtime = None
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            run(core.saveSession(session))
            run(core.saveTaskRecord({"id": "stale-runtime-record", "sessionId": session["id"], "kind": "background_command", "status": "running", "title": "stale cache record", "updatedAt": "2026-01-01T00:00:09Z"}))

            task_id = "live-main-runtime"
            output_file = core.taskOutputPath(task_id)
            Path(output_file).parent.mkdir(parents=True, exist_ok=True)
            Path(output_file).write_text("", encoding="utf-8")
            session["metadata"]["taskIds"] = [task_id]
            session["updatedAt"] = "2026-01-01T00:00:10Z"
            session["messages"].append({"role": "user", "content": "Continue with Slice 8."})
            session["messages"].append({"role": "assistant", "content": "Live in-memory session state is the source of truth."})
            run(core.writeTaskMeta(task_id, {"type": "main_session", "taskId": task_id, "sessionId": session["id"], "command": "continue", "cwd": str(workspace), "startedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:01Z", "status": "running", "outputFile": output_file, "attachableSessionId": session["id"]}))

            runtime = run(core.createTaskRuntime({"session": session, "config": config, "emit": lambda *_: None, "startTicker": False}))
            snapshot = runtime["getSnapshot"]()
            ids = [task["id"] for task in snapshot["tasks"]]
            self.assertIn(task_id, ids)
            self.assertNotIn("stale-runtime-record", ids)
            task = next(item for item in snapshot["tasks"] if item["id"] == task_id)
            self.assertEqual(task["summary"], "This session is continuing in the background. Latest assistant state: Live in-memory session state is the source of truth.")
            persisted = run(core.loadSession(session["id"]))
            self.assertEqual(persisted["messages"][-1]["content"], "Live in-memory session state is the source of truth.")
        finally:
            if runtime:
                run(runtime["close"]())
            self.restore_home(root, previous)

    def test_task_supervisor_projection_matches_reference_task_family_details(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            session["updatedAt"] = "2026-01-01T00:00:05Z"
            session["messages"].append({"role": "user", "content": "Build the parity slice."})
            session["messages"].append({"role": "assistant", "content": "The detached worker is synthesizing the result."})
            background_id = "projection-background"
            main_id = "projection-main"
            health_id = "projection-health"
            session["metadata"]["taskIds"] = [background_id, main_id, health_id]
            run(core.saveSession(session))
            background_output = core.taskOutputPath(background_id)
            main_output = core.taskOutputPath(main_id)
            health_output = core.taskOutputPath(health_id)
            for output_file, text in [(background_output, "old line\nlatest output line\n"), (main_output, ""), (health_output, "timeout\n")]:
                Path(output_file).parent.mkdir(parents=True, exist_ok=True)
                Path(output_file).write_text(text, encoding="utf-8")
            run(core.writeTaskMeta(background_id, {"type": "background_command", "taskId": background_id, "sessionId": session["id"], "command": "python -m tests", "cwd": str(workspace), "startedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:01Z", "status": "running", "outputFile": background_output, "pid": os.getpid(), "owner": "daemon"}))
            run(core.writeTaskMeta(main_id, {"type": "main_session", "taskId": main_id, "sessionId": session["id"], "command": "continue", "cwd": str(workspace), "startedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:01Z", "status": "running", "outputFile": main_output, "attachableSessionId": session["id"], "owner": "daemon"}))
            run(core.writeTaskMeta(health_id, {"type": "health_check", "taskId": health_id, "sessionId": session["id"], "cwd": str(workspace), "title": "API health", "url": "http://localhost:9", "intervalMs": 5000, "timeoutMs": 1000, "backoffMultiplier": 2, "maxIntervalMs": 20000, "failureThreshold": 2, "status": "unhealthy", "createdAt": "2026-01-01T00:00:00Z", "startedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:02Z", "lastError": "timeout", "lastStatusCode": 503, "consecutiveFailures": 1, "outputFile": health_output}))

            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace)}))
            bootstrap = run(supervisor["getBootstrap"](session["id"]))
            tasks = {task["id"]: task for task in bootstrap["taskSnapshot"]["tasks"]}
            background = tasks[background_id]
            self.assertEqual(background["status"], "backgrounded")
            self.assertEqual(background["summary"], "Background command is active. Latest output: latest output line")
            self.assertEqual(background["detailStatus"], "streaming_output")
            self.assertEqual(background["resultSummary"], "latest output line")
            self.assertEqual(background["metadata"]["output_preview"], "latest output line")
            self.assertEqual(background["metadata"]["detached_type"], "background_command")
            detached = tasks[main_id]
            self.assertEqual(detached["taskFamily"], "detached_session")
            self.assertEqual(detached["summary"], "This session is continuing in the background. Latest assistant state: The detached worker is synthesizing the result.")
            self.assertEqual(detached["detailStatus"], "continuing_session")
            self.assertEqual(detached["resultSummary"], "The detached worker is synthesizing the result.")
            self.assertFalse(detached["retrieveOnly"])
            health = tasks[health_id]
            self.assertEqual(health["status"], "degraded")
            self.assertEqual(health["summary"], "Check unhealthy: timeout")
            self.assertFalse(health["foregroundable"])
            self.assertEqual(health["detailStatus"], "unhealthy")
            self.assertEqual(health["resultSummary"], "timeout")
            self.assertEqual(health["metadata"]["current_interval_ms"], 5000)
            restored = run(core.loadSession(session["id"]))["metadata"]["runtimeRestore"]
            self.assertEqual(restored["restoredTaskCount"], 3)
        finally:
            if supervisor:
                run(supervisor["close"]())
            self.restore_home(root, previous)
