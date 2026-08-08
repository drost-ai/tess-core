from __future__ import annotations

from tests.support.agent_core_case import *

class ToolAgentTests(AgentCoreTestCase):
    def test_agent_tool_prepares_child_session_and_record(self):
        root, home, workspace, previous = self.temp_context()
        try:
            from agent_core.tasks.subagents import prepareSubagent

            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "acceptEdits"}, str(workspace)))
            config["systemPrompt"] = "Config level prompt."
            config["agents"] = {"researcher": {"prompt": "Research only.", "model": "gpt-mini"}}
            parent = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="acceptEdits")
            parent["systemPrompt"] = "Parent session prompt."
            agent = next(tool for tool in core.createBuiltinTools() if tool.name == "Agent")
            self.assertEqual(agent.inputSchema["required"], ["description", "prompt"])
            self.assertIn("subagent_type", agent.inputSchema["properties"])
            self.assertNotIn("run_in_background", agent.inputSchema["properties"])
            persisted = {"count": 0}
            context = {
                "session": parent,
                "config": config,
                "interactive": False,
                "askQuestion": lambda *_: "",
                "outputJson": lambda *_: None,
                "cwd": str(workspace),
                "persistSession": lambda: persisted.__setitem__("count", persisted["count"] + 1),
            }
            prepared = run(prepareSubagent({
                "context": context,
                "description": "Investigate",
                "prompt": "Find the issue",
                "agentType": "researcher",
                "mode": "default",
                "teamName": "alpha",
                "runInBackground": True,
            }))
            child_session = prepared["childSession"]
            self.assertEqual(
                (child_session["messages"][0]["content"], child_session["messages"][0]["metadata"]["agentCoreOrigin"]["kind"]),
                ("Find the issue", "subagent_task_prompt"),
            )
            self.assertEqual(child_session["permissionMode"], "default")
            self.assertEqual(child_session["metadata"]["depth"], 1)
            self.assertEqual(child_session["metadata"]["parentSessionId"], parent["id"])
            self.assertNotIn("assistantName", child_session["metadata"])
            self.assertEqual(prepared["childConfig"]["agent"], "researcher")
            self.assertEqual(prepared["childConfig"]["model"], "gpt-5-mini-2025-08-07")
            self.assertTrue(prepared["childConfig"]["systemPrompt"].startswith("Config level prompt."))
            self.assertEqual(child_session["systemPrompt"], prepared["childConfig"]["systemPrompt"])
            self.assertIn("# Agent Instructions\nResearch only.", prepared["childConfig"]["systemPrompt"])
            self.assertEqual(parent["metadata"]["childAgentIds"], [prepared["agentId"]])
            self.assertEqual(parent["metadata"]["childSessionIds"], [child_session["id"]])
            child_record = run(core.loadChildAgent(prepared["agentId"]))
            self.assertEqual(child_record["teamName"], "alpha")
            self.assertTrue(child_record["runInBackground"])

            unknown = run(agent.handler({"description": "Nope", "prompt": "x", "subagent_type": "missing"}, dict(context)))
            self.assertTrue(unknown["isError"])
            self.assertIn("Unknown subagent type: missing", unknown["content"])
            self.assertIn("Configured types: researcher", unknown["content"])

            no_profiles = run(agent.handler(
                {"description": "Nope", "prompt": "x", "subagent_type": "general-purpose"},
                {**context, "config": {**config, "agents": {}}},
            ))
            self.assertTrue(no_profiles["isError"])
            self.assertIn("no agent profiles configured", no_profiles["content"])
            self.assertIn("retry without subagent_type", no_profiles["content"])
            self.assertIn(
                "OMIT unless the runtime has agent profiles configured",
                agent.inputSchema["properties"]["subagent_type"]["description"],
            )

            auto_config = {**config, "permissionMode": "auto"}
            auto_parent = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="auto")
            auto_prepared = run(prepareSubagent({
                "context": {**context, "session": auto_parent, "config": auto_config},
                "description": "Invalid mode",
                "prompt": "inherit auto",
                "mode": None,
                "runInBackground": True,
            }))
            self.assertEqual(auto_prepared["childSession"]["permissionMode"], "auto")
        finally:
            self.restore_home(root, previous)

    def test_agent_tool_background_without_supervisor_fails_loudly(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            parent = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            run(core.saveSession(parent))
            agent = next(tool for tool in core.createBuiltinTools() if tool.name == "Agent")
            with self.assertRaises(RuntimeError) as raised:
                run(
                    agent.handler(
                        {"description": "No daemon", "prompt": "Run without a supervisor", "run_in_background": True},
                        {"session": parent, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None},
                    )
                )
            self.assertIn("task supervisor daemon", str(raised.exception))
        finally:
            self.restore_home(root, previous)

    def test_background_subagent_finalize_env_fallback_records_match_reference(self):
        root, home, workspace, previous = self.temp_context()
        previous_env = {name: os.environ.get(name) for name in ("TESS_CHILD_AGENT_ID", "TESS_CHILD_TASK_ID", "TESS_CHILD_SESSION_ID")}
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            session["metadata"]["parentSessionId"] = "parent-env"
            os.environ["TESS_CHILD_AGENT_ID"] = "agent-env-success"
            os.environ["TESS_CHILD_TASK_ID"] = "task-env-success"
            os.environ["TESS_CHILD_SESSION_ID"] = session["id"]
            run(core.finalizeBackgroundSubagentSuccessFromEnv(session, {"text": "finished", "stopReason": "end_turn"}))
            success_child = run(core.loadChildAgent("agent-env-success"))
            self.assertEqual(success_child["parentSessionId"], "parent-env")
            self.assertEqual(success_child["status"], "completed")
            self.assertEqual(success_child["completionReason"], "worker_completed")
            self.assertEqual(success_child["owner"], "daemon")
            success_meta = run(core.readTaskMeta("task-env-success"))
            self.assertEqual(success_meta["owner"], "daemon")
            self.assertEqual(success_meta["completionReason"], "worker_completed")

            os.environ["TESS_CHILD_AGENT_ID"] = "agent-env-failure"
            os.environ["TESS_CHILD_TASK_ID"] = "task-env-failure"
            os.environ["TESS_CHILD_SESSION_ID"] = "session-env-failure"
            run(core.finalizeBackgroundSubagentFailureFromEnv(RuntimeError("boom")))
            failure_child = run(core.loadChildAgent("agent-env-failure"))
            self.assertEqual(failure_child["parentSessionId"], "")
            self.assertEqual(failure_child["provider"], "anthropic")
            self.assertEqual(failure_child["mode"], "default")
            self.assertEqual(failure_child["status"], "failed")
            self.assertEqual(failure_child["completionReason"], "worker_failed")
            failure_meta = run(core.readTaskMeta("task-env-failure"))
            self.assertEqual(failure_meta["error"], "boom")
            self.assertEqual(failure_meta["owner"], "daemon")
        finally:
            for name, value in previous_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            self.restore_home(root, previous)

    def test_agent_tool_background_registers_subagent_task_metadata(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            parent = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            run(core.saveSession(parent))
            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace)}))
            events = []
            agent = next(tool for tool in core.createBuiltinTools() if tool.name == "Agent")
            result = run(
                agent.handler(
                    {"description": "Run async", "prompt": "Work in the background", "name": "Async worker", "wait": True},
                    {"session": parent, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda event, data: events.append((event, data)), "cwd": str(workspace), "persistSession": lambda: None, "taskSupervisor": supervisor},
                )
            )
            payload = json.loads(result["content"])
            task_id = payload["task_id"]
            self.assertEqual(payload["status"], "idle_resident")
            self.assertIn("No credential is configured", payload["result"])
            self.assertEqual(parent["metadata"]["backgroundSessionIds"], [payload["session_id"]])
            self.assertEqual(parent["metadata"]["childAgentIds"], [payload["agent_id"]])
            child_record = run(core.loadChildAgent(payload["agent_id"]))
            self.assertEqual(child_record["status"], "awaiting_input")
            self.assertEqual(child_record["taskId"], task_id)
            self.assertTrue(child_record["stoppable"])
            meta = run(core.readTaskMeta(task_id))
            self.assertEqual(meta["type"], "subagent")
            self.assertEqual(meta["agent_id"], payload["agent_id"])
            self.assertEqual(meta["session_id"], payload["session_id"])
            self.assertEqual(meta["parent_session_id"], parent["id"])
            task_record = run(supervisor["inspectTask"](task_id))
            self.assertEqual(task_record["kind"], "subagent")
            self.assertEqual(task_record["sessionId"], parent["id"])
            self.assertEqual(task_record["sourceId"], payload["agent_id"])
            self.assertEqual(task_record["attachableSessionId"], payload["session_id"])
            self.assertEqual(task_record["taskFamily"], "subagent")
            self.assertTrue(any(event == "subagent.backgrounded" and data["task_id"] == task_id for event, data in events))
        finally:
            if supervisor:
                run(supervisor["close"]())
            self.restore_home(root, previous)

    def test_background_subagent_worker_runs_and_finalizes_metadata(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            parent = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            child = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            child["messages"].append({"role": "user", "content": "finish without tools"})
            child["metadata"]["parentSessionId"] = parent["id"]
            child["metadata"]["agentId"] = "agent-worker"
            child["metadata"]["agentName"] = "Worker"
            child["metadata"]["subagentRuntimeContract"] = {"resident": False}
            child_authority = run(
                core.captureAgentAuthority(
                    config=config,
                    session=child,
                    tools=core.createBuiltinTools(),
                )
            )
            child["metadata"]["effectiveAgentAuthority"] = child_authority
            run(core.saveSession(parent))
            run(core.saveSession(child))
            run(
                core.saveChildAgent(
                    {
                        "agentId": "agent-worker",
                        "sessionId": child["id"],
                        "parentSessionId": parent["id"],
                        "name": "Worker",
                        "provider": config["provider"],
                        "model": config["model"],
                        "mode": "bypassPermissions",
                        "runInBackground": True,
                        "status": "queued",
                        "startedAt": "2026-01-01T00:00:00Z",
                        "owner": "daemon",
                        "attachableSessionId": child["id"],
                        "effectiveAgentAuthority": child_authority,
                    }
                )
            )
            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace)}))
            started = run(supervisor["startSubagentTask"]({"parentSessionId": parent["id"], "sessionId": child["id"], "agentId": "agent-worker", "cwd": str(workspace), "label": "Worker"}))
            task_id = started["taskId"]
            for _ in range(100):
                meta = run(core.readTaskMeta(task_id))
                if meta and meta.get("status") == "completed":
                    break
                time.sleep(0.05)
            meta = run(core.readTaskMeta(task_id))
            self.assertEqual(meta["type"], "subagent")
            self.assertEqual(meta["status"], "completed", meta)
            self.assertEqual(meta["agent_id"], "agent-worker")
            self.assertEqual(meta["session_id"], child["id"])
            self.assertEqual(meta["completionReason"], "worker_completed")
            child_record = run(core.loadChildAgent("agent-worker"))
            self.assertEqual(child_record["status"], "completed")
            self.assertEqual(child_record["taskId"], task_id)
            self.assertFalse(child_record["stoppable"])
            self.assertEqual(child_record["completionReason"], "worker_completed")
            output = run(supervisor["readTaskOutput"](task_id, 12000))
            self.assertIn('"ok": true', output["output"])
            self.assertIn("No credential is configured", output["output"])
            task_record = None
            for _ in range(40):
                task_record = run(supervisor["inspectTask"](task_id))
                if task_record and task_record.get("status") == "completed":
                    break
                time.sleep(0.05)
            self.assertEqual(task_record["kind"], "subagent")
            self.assertEqual(task_record["status"], "completed")
            self.assertEqual(task_record["metadata"]["agent_id"], "agent-worker")
        finally:
            if supervisor:
                run(supervisor["close"]())
            self.restore_home(root, previous)

    def test_detached_main_session_task_runs_worker_and_retries_as_main_session(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            run(core.saveSession(session))
            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace)}))
            prompt = "finish detached main-session work"
            started = run(supervisor["startMainSessionTask"]({"sessionId": session["id"], "cwd": str(workspace), "prompt": prompt, "title": "Detached check"}))
            task_id = started["taskId"]
            for _ in range(100):
                meta = run(core.readTaskMeta(task_id))
                if meta and meta.get("status") == "completed":
                    break
                time.sleep(0.05)
            meta = run(core.readTaskMeta(task_id))
            self.assertEqual(meta["type"], "main_session")
            self.assertEqual(meta["status"], "completed")
            self.assertEqual(meta["sessionId"], session["id"])
            self.assertEqual(meta["command"], prompt)
            output = run(supervisor["readTaskOutput"](task_id, 12000))
            self.assertIn('"ok": true', output["output"])
            self.assertIn("No credential is configured", output["output"])
            task_record = None
            for _ in range(40):
                task_record = run(supervisor["inspectTask"](task_id))
                if task_record and task_record.get("status") == "completed":
                    break
                time.sleep(0.05)
            self.assertEqual(task_record["kind"], "main_session")
            self.assertEqual(task_record["taskFamily"], "detached_session")
            self.assertFalse(task_record["retrieveOnly"])
            self.assertEqual(task_record["attachableSessionId"], session["id"])
            persisted = run(core.loadSession(session["id"]))
            self.assertIn(task_id, persisted["metadata"]["taskIds"])
            self.assertIn(session["id"], persisted["metadata"]["backgroundSessionIds"])
            self.assertTrue(any(message.get("role") == "user" and message.get("content") == prompt for message in persisted["messages"]))
            retried = run(supervisor["retryTask"](task_id))
            retry_id = retried["taskId"]
            self.assertNotEqual(retry_id, task_id)
            for _ in range(100):
                retry_meta = run(core.readTaskMeta(retry_id))
                if retry_meta and retry_meta.get("status") == "completed":
                    break
                time.sleep(0.05)
            retry_record = run(supervisor["inspectTask"](retry_id))
            self.assertEqual(retry_record["kind"], "main_session")
            self.assertEqual(retry_record["command"], prompt)
        finally:
            if supervisor:
                run(supervisor["close"]())
            self.restore_home(root, previous)

    def test_task_supervisor_retry_records_lineage_and_rejects_unsupported_kind(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            run(core.saveSession(session))
            original_id = "retry-original-task"
            output_file = core.taskOutputPath(original_id)
            Path(output_file).parent.mkdir(parents=True, exist_ok=True)
            Path(output_file).write_text("old\n", encoding="utf-8")
            retry_script = 'print("retried")'
            retry_command = f"{shlex.quote(sys.executable)} -c {shlex.quote(retry_script)}"
            run(core.writeTaskMeta(original_id, {"type": "background_command", "taskId": original_id, "id": original_id, "sessionId": session["id"], "command": retry_command, "cwd": str(workspace), "startedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:01Z", "finishedAt": "2026-01-01T00:00:01Z", "status": "failed", "outputFile": output_file, "owner": "daemon", "metadata": {"retry_attempt": 2, "retry_root_task_id": "root-task"}}))
            run(core.saveTaskRecord({"id": original_id, "sessionId": session["id"], "sourceId": original_id, "kind": "background_command", "status": "failed", "title": "retry original", "summary": "failed", "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:01Z", "startedAt": "2026-01-01T00:00:00Z", "cwd": str(workspace), "command": retry_command, "outputFile": output_file, "foregroundMode": "background", "taskFamily": "shell", "retrieveOnly": True, "owner": "daemon", "metadata": {"retry_attempt": 2, "retry_root_task_id": "root-task"}}))
            run(core.saveTaskRecord({"id": "retry-monitor", "sessionId": session["id"], "sourceId": "retry-monitor", "kind": "monitor", "status": "running", "title": "monitor", "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z", "startedAt": "2026-01-01T00:00:00Z", "foregroundable": False, "restorable": True}))
            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace)}))
            retried = run(supervisor["retryTask"](original_id))
            retry_id = retried["taskId"]
            self.assertNotEqual(retry_id, original_id)
            retry_meta = run(core.readTaskMeta(retry_id))
            retry_record = run(supervisor["inspectTask"](retry_id))
            self.assertEqual(retry_meta["retryOfTaskId"], original_id)
            self.assertEqual(retry_meta["retryRootTaskId"], "root-task")
            self.assertEqual(retry_meta["retryAttempt"], 3)
            self.assertEqual(retry_meta["metadata"]["retry_of_task_id"], original_id)
            self.assertEqual(retry_record["metadata"]["retry_attempt"], 3)
            with self.assertRaises(RuntimeError) as ctx:
                run(supervisor["retryTask"]("retry-monitor"))
            self.assertIn("Retry is not supported for task kind: monitor", str(ctx.exception))
        finally:
            if supervisor:
                run(supervisor["close"]())
            self.restore_home(root, previous)


class NonBlockingSpawnTests(AgentCoreTestCase):
    def test_default_spawn_returns_immediately_without_waiting(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            parent = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            run(core.saveSession(parent))

            async def fake_start(task_args):
                return {"taskId": "nb-task", "outputFile": str(workspace / "out.log")}

            started_at = time.monotonic()
            result = run(
                next(tool for tool in core.createBuiltinTools() if tool.name == "Agent").handler(
                    {"description": "No wait", "prompt": "go"},
                    {"session": parent, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None, "taskSupervisor": {"startSubagentTask": fake_start}},
                )
            )
            self.assertLess(time.monotonic() - started_at, 2.0, "default spawn must not block on the child")
            payload = json.loads(result["content"])
            self.assertEqual(payload["task_id"], "nb-task")
            self.assertIsNone(payload["result"])
            self.assertIn("not waiting", payload["note"])
        finally:
            self.restore_home(root, previous)
