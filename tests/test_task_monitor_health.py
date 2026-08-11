from __future__ import annotations

from tests.support.agent_core_case import *
from agent_core.tasks.health_check_policy import (
    authorizeHealthCheckSpec,
    healthCheckSpecDigest,
    normalizeHealthCheckSpec,
)


async def _start_authorized_health_check(
    supervisor: dict,
    session: dict,
    config: dict,
    workspace: Path,
    request: dict,
) -> dict:
    spec = await normalizeHealthCheckSpec(request, str(workspace))
    execution_policy = await authorizeHealthCheckSpec(
        {
            "session": session,
            "config": config,
            "interactive": False,
            "outputJson": lambda *_args: None,
            "persistSession": lambda: None,
        },
        spec,
    )
    return await supervisor["startHealthCheck"](
        {
            "sessionId": session["id"],
            "spec": spec,
            "specDigest": healthCheckSpecDigest(spec),
            "executionPolicy": execution_policy,
        }
    )


class TaskMonitorHealthTests(AgentCoreTestCase):
    def setUp(self):
        super().setUp()
        # HTTP health checks target loopback fixtures; the fetch plane's
        # private-address block requires the explicit opt-in knob.
        env = mock.patch.dict(os.environ, {"AGENT_CORE_WEB_FETCH_PRIVATE_HOST_ALLOWLIST": "127.0.0.1,localhost"})
        env.start()
        self.addCleanup(env.stop)

    def test_task_monitor_and_activity_descriptors_match_reference_shape(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            task_id = "monitor-task"
            session["metadata"]["taskIds"] = [task_id]
            output_file = core.task_output_path(task_id)
            run(
                core.write_task_meta(
                    task_id,
                    {
                        "type": "background_command",
                        "taskId": task_id,
                        "id": task_id,
                        "sessionId": session["id"],
                        "command": "python -m server",
                        "cwd": str(workspace),
                        "startedAt": "2026-01-01T00:00:00Z",
                        "updatedAt": "2026-01-01T00:00:00Z",
                        "status": "running",
                        "outputFile": output_file,
                    },
                )
            )
            monitor = next(tool for tool in core.createBuiltinTools() if tool.name == "TaskMonitorStart")
            created = run(monitor.handler({"task_id": task_id, "goal": "watch server", "summary_style": "brief", "check_interval_ms": 1000}, {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None}))
            payload = json.loads(created["content"])
            self.assertEqual(payload["task_id"], task_id)
            self.assertEqual(payload["execution"]["title"], f"Monitoring task {task_id}")
            self.assertEqual(payload["execution"]["metadata"]["summary_style"], "brief")
            self.assertIn(payload["monitor_id"], session["metadata"]["monitorIds"])
            self.assertEqual(
                core.formatMonitorSummary([{"id": "monitor-123456", "status": "active", "taskId": task_id, "goal": "watch server", "lastAssistantUpdateAt": "2026-01-01T00:00:00Z"}]),
                "monitor-  active · task monitor- · watch server · last update 2026-01-01T00:00:00Z",
            )
            self.assertEqual(core.defaultMonitorCheckIntervalMs(math.inf), 5000)
            self.assertEqual(core.defaultMonitorHeartbeatIntervalMs(math.inf), 60000)
            self.assertEqual(core.defaultMonitorCheckIntervalMs(True), 5000)

            plugin_descriptors = core.createPluginActivityDescriptors(
                [
                    {"id": "alpha", "diagnostics": [], "tools": [1, 2], "commands": [1], "mcpServers": [], "source": "local", "capabilities": {"tools": True}},
                    {"id": "beta", "diagnostics": [{"severity": "warning"}], "tools": [], "commands": [], "mcpServers": [1]},
                    {"id": "gamma", "diagnostics": [{"severity": "error"}], "tools": [], "commands": [], "mcpServers": []},
                ]
            )
            self.assertEqual(plugin_descriptors[0]["status"], "ok")
            self.assertEqual(plugin_descriptors[0]["summary"], "alpha provides 2 tools, 1 commands, and 0 MCP servers")
            self.assertEqual(plugin_descriptors[1]["status"], "warn")
            self.assertEqual(plugin_descriptors[2]["status"], "error")

            mcp_descriptors = core.createMcpActivityDescriptors(
                [
                    {"name": "stdio", "connected": True, "state": "connected", "tools": 2, "resources": 1, "transport": "stdio"},
                    {"name": "sse", "connected": False, "state": "degraded", "tools": [], "resources": [], "error": "lost"},
                    {"name": "http", "connected": False, "state": "disconnected", "tools": [], "resources": []},
                ]
            )
            self.assertEqual([item["status"] for item in mcp_descriptors], ["ok", "warn", "error"])
            self.assertEqual(mcp_descriptors[0]["summary"], "stdio is connected with 2 tools and 1 resources")
            self.assertEqual(mcp_descriptors[1]["metadata"]["error"], "lost")
        finally:
            self.restore_home(root, previous)

    def test_monitor_runtime_bootstrap_emits_state_without_assistant_update(self):
        root, home, workspace, previous = self.temp_context()
        runtime = None
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            run(core.save_session(session))
            task_id = "monitor-runtime-task"
            output_file = core.task_output_path(task_id)
            Path(output_file).parent.mkdir(parents=True, exist_ok=True)
            Path(output_file).write_text("starting\n", encoding="utf-8")
            run(core.write_task_meta(task_id, {"type": "background_command", "taskId": task_id, "id": task_id, "sessionId": session["id"], "command": "python worker.py", "cwd": str(workspace), "startedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z", "status": "running", "outputFile": output_file}))
            monitor = core.createMonitorRecord({"sessionId": session["id"], "taskId": task_id, "goal": "watch runtime monitor", "checkIntervalMs": 2000})
            run(core.save_monitor(monitor))
            events = []
            runtime = run(core.createMonitorRuntime({"session": session, "config": config, "emit": lambda event, data: events.append((event, data)), "tickIntervalMs": 50}))
            self.assertEqual(runtime["getSnapshot"]()["active"], 1)
            self.assertTrue(any(event == "monitor.snapshot" for event, _ in events))
            self.assertTrue(any(event == "monitor.status_changed" and data["task_id"] == task_id for event, data in events))
            self.assertFalse(any(event == "monitor.assistant_update" for event, _ in events))
            self.assertEqual(session["messages"], [])
            saved = run(core.load_monitor(monitor["id"]))
            self.assertNotIn("lastAssistantUpdateSummary", saved)
            self.assertIn(monitor["id"], session["metadata"]["monitorIds"])
        finally:
            if runtime:
                run(runtime["close"]())
            self.restore_home(root, previous)

    def test_monitor_runtime_uses_provider_authored_update_and_usage(self):
        root, home, workspace, previous = self.temp_context()
        runtime = None
        original_send = monitors_module.sendOpenAiRequest
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            config["openaiToken"] = "test-token"
            session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            session["messages"].append({"role": "user", "content": "Please monitor the build."})
            run(core.save_session(session))
            task_id = "monitor-provider-task"
            output_file = core.task_output_path(task_id)
            Path(output_file).parent.mkdir(parents=True, exist_ok=True)
            Path(output_file).write_text("pytest started\n", encoding="utf-8")
            run(core.write_task_meta(task_id, {"type": "background_command", "taskId": task_id, "id": task_id, "sessionId": session["id"], "command": "pytest", "cwd": str(workspace), "startedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z", "status": "running", "outputFile": output_file}))
            monitor = core.createMonitorRecord({"sessionId": session["id"], "taskId": task_id, "goal": "watch provider monitor", "summaryStyle": "brief", "checkIntervalMs": 2000})
            monitor["checkIntervalMs"] = 1
            monitor["createdAt"] = "2026-01-01T00:00:00Z"
            monitor["updatedAt"] = "2026-01-01T00:00:00Z"
            run(core.save_monitor(monitor))
            calls = {}

            async def fake_send(args):
                calls["args"] = args
                return {"content": [{"type": "text", "text": "Provider says the build monitor is live."}], "usage": {"input_tokens": 7, "output_tokens": 3, "cache_creation_input_tokens": 2, "cache_read_input_tokens": 1}}

            monitors_module.sendOpenAiRequest = fake_send
            events = []
            async def exercise_runtime():
                created = await core.createMonitorRuntime({"session": session, "config": config, "emit": lambda event, data: events.append((event, data)), "tickIntervalMs": 10})
                try:
                    self.assertNotIn("args", calls)
                    with Path(output_file).open("a", encoding="utf-8") as handle:
                        handle.write("x" * 256 + "\n")
                    for _ in range(100):
                        if any(event == "monitor.assistant_update" for event, _ in events):
                            break
                        await asyncio.sleep(0.02)
                    return created
                finally:
                    await created["close"]()

            runtime = run(exercise_runtime())
            update = next(data for event, data in events if event == "monitor.assistant_update")
            self.assertEqual(update["text"], "Provider says the build monitor is live.")
            request = calls["args"]["request"]
            self.assertEqual(request["tools"], [])
            payload = json.loads(request["messages"][0]["content"])
            self.assertEqual(payload["reason"], "progress")
            self.assertEqual(payload["goal"], "watch provider monitor")
            self.assertEqual(payload["summary_style"], "brief")
            self.assertIn("x" * 64, payload["new_output_excerpt"])
            self.assertEqual(session["metadata"]["usage"]["inputTokens"], 7)
            self.assertEqual(session["metadata"]["usage"]["outputTokens"], 3)
            self.assertEqual(session["metadata"]["usage"]["cacheCreationInputTokens"], 2)
            self.assertEqual(session["metadata"]["usage"]["cacheReadInputTokens"], 1)
            saved_session = run(core.load_session(session["id"]))
            self.assertEqual(saved_session["messages"][-1]["content"], "Provider says the build monitor is live.")
        finally:
            monitors_module.sendOpenAiRequest = original_send
            if runtime:
                run(runtime["close"]())
            self.restore_home(root, previous)

    def test_health_check_threshold_backoff_and_recovery(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                config = await core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace))
                session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
                await core.save_session(session)
                counter = workspace / "health-count.txt"
                script = textwrap.dedent(
                    f"""
                    from pathlib import Path
                    p = Path({str(counter)!r})
                    n = int(p.read_text()) if p.exists() else 0
                    p.write_text(str(n + 1))
                    print(f"health-attempt-{{n + 1}}")
                    raise SystemExit(1 if n < 2 else 0)
                    """
                )
                command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
                events = []
                supervisor = await core.createTaskSupervisor({"cwd": str(workspace), "onEvent": lambda event: events.append(event)})
                try:
                    started = await _start_authorized_health_check(
                        supervisor,
                        session,
                        config,
                        workspace,
                        {
                            "cwd": str(workspace),
                            "title": "health threshold",
                            "mode": "command",
                            "command": command,
                            "intervalMs": 1000,
                            "timeoutMs": 1000,
                            "failureThreshold": 3,
                            "backoffMultiplier": 2,
                            "maxIntervalMs": 1000,
                        }
                    )
                    task_id = started["taskId"]
                    health = None
                    for _ in range(120):
                        health = await core.load_health_check_task(task_id)
                        if health and health.get("status") == "active" and health.get("lastSuccessAt"):
                            break
                        await asyncio.sleep(0.05)
                    output = await supervisor["readTaskOutput"](task_id, 12000)
                    task_record = await supervisor["inspectTask"](task_id)
                    persisted = await core.load_session(session["id"])
                    await supervisor["stopTask"](task_id)
                    return {"health": health, "events": events, "output": output, "taskRecord": task_record, "persisted": persisted, "taskId": task_id}
                finally:
                    await supervisor["close"]()

            result = run(scenario())
            health = result["health"]
            task_id = result["taskId"]
            self.assertEqual(health["status"], "active")
            self.assertEqual(health["consecutiveFailures"], 0)
            self.assertEqual(health["currentIntervalMs"], 1000)
            self.assertEqual(health["lastExitCode"], 0)
            self.assertIn(task_id, result["persisted"]["metadata"]["taskIds"])
            unhealthy_failures = [event["data"]["failures"] for event in result["events"] if event["event"] == "health_check.unhealthy"]
            self.assertEqual(unhealthy_failures[:2], [1, 2])
            self.assertTrue(any(event["event"] == "health_check.recovered" and event["data"]["task_id"] == task_id for event in result["events"]))
            output = result["output"]
            self.assertIn("health-attempt-1", output["output"])
            self.assertIn("health-attempt-2", output["output"])
            self.assertIn("health-attempt-3", output["output"])
            task_record = result["taskRecord"]
            self.assertEqual(task_record["kind"], "health_check")
            self.assertEqual(task_record["taskFamily"], "health_check")
            self.assertEqual(task_record["status"], "running")
        finally:
            self.restore_home(root, previous)

    def test_health_check_http_body_fixtures_and_timeout_details(self):
        root, home, workspace, previous = self.temp_context()
        server = None
        try:
            class HealthHandler(http.server.BaseHTTPRequestHandler):
                calls = 0

                def log_message(self, *_args):
                    return

                def do_GET(self):
                    HealthHandler.calls += 1
                    if HealthHandler.calls == 1:
                        self.send_response(503)
                        body = b"not ready"
                    elif HealthHandler.calls == 2:
                        self.send_response(202)
                        body = b"wrong body"
                    else:
                        self.send_response(202)
                        body = b"ready body"
                    self.end_headers()
                    self.wfile.write(body)

            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            async def scenario():
                config = await core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace))
                session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
                await core.save_session(session)
                events = []
                supervisor = await core.createTaskSupervisor({"cwd": str(workspace), "onEvent": lambda event: events.append(event)})
                try:
                    http_started = await _start_authorized_health_check(
                        supervisor,
                        session,
                        config,
                        workspace,
                        {
                            "cwd": str(workspace),
                            "title": "http body",
                            "mode": "http",
                            "url": f"http://127.0.0.1:{server.server_address[1]}/health",
                            "expectedStatusCodes": [202],
                            "expectedBodyPolicy": "contains",
                            "expectedBodySubstring": "ready",
                            "intervalMs": 1000,
                            "timeoutMs": 1000,
                            "failureThreshold": 4,
                        }
                    )
                    http_task_id = http_started["taskId"]
                    http_record = None
                    for _ in range(120):
                        http_record = await core.load_health_check_task(http_task_id)
                        if http_record and http_record.get("status") == "active" and http_record.get("lastSuccessAt"):
                            break
                        await asyncio.sleep(0.05)
                    timeout_started = await _start_authorized_health_check(
                        supervisor,
                        session,
                        config,
                        workspace,
                        {
                            "cwd": str(workspace),
                            "title": "timeout body",
                            "mode": "command",
                            "command": f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(2)')}",
                            "intervalMs": 1000,
                            "timeoutMs": 1000,
                            "failureThreshold": 1,
                        }
                    )
                    timeout_task_id = timeout_started["taskId"]
                    timeout_record = None
                    for _ in range(80):
                        timeout_record = await core.load_health_check_task(timeout_task_id)
                        if timeout_record and timeout_record.get("status") == "failed":
                            break
                        await asyncio.sleep(0.05)
                    http_output = await supervisor["readTaskOutput"](http_task_id, 12000)
                    timeout_output = await supervisor["readTaskOutput"](timeout_task_id, 12000)
                    await supervisor["stopTask"](http_task_id)
                    return {"events": events, "http": http_record, "timeout": timeout_record, "httpOutput": http_output, "timeoutOutput": timeout_output, "httpTaskId": http_task_id, "timeoutTaskId": timeout_task_id}
                finally:
                    await supervisor["close"]()

            result = run(scenario())
            http_record = result["http"]
            timeout_record = result["timeout"]
            self.assertEqual(http_record["status"], "active")
            self.assertEqual(http_record["lastStatusCode"], 202)
            self.assertEqual(http_record["lastResponseSnippet"], "ready body")
            self.assertIn("HTTP 503", result["httpOutput"]["output"])
            self.assertIn("wrong body", result["httpOutput"]["output"])
            self.assertIn("ready body", result["httpOutput"]["output"])
            unhealthy_errors = [event["data"]["error"] for event in result["events"] if event["event"] == "health_check.unhealthy" and event["data"]["task_id"] == result["httpTaskId"]]
            self.assertEqual(unhealthy_errors[:2], ["Unexpected status code 503.", "Response body did not contain expected text: ready"])
            self.assertTrue(any(event["event"] == "health_check.recovered" and event["data"]["task_id"] == result["httpTaskId"] for event in result["events"]))
            direct_task_events = [event for event in result["events"] if event["data"].get("task_id") in {result["httpTaskId"], result["timeoutTaskId"]} and event["event"].startswith("task.")]
            self.assertEqual(len(direct_task_events), 1)
            self.assertEqual(direct_task_events[0]["event"], "task.stopped")
            self.assertEqual(direct_task_events[0]["data"]["task_id"], result["httpTaskId"])
            self.assertEqual(direct_task_events[0]["data"]["detail_status"], "stopped_by_request")
            self.assertEqual(timeout_record["status"], "failed")
            self.assertTrue(timeout_record["lastTimedOut"])
            self.assertTrue(timeout_record["lastTerminated"])
            self.assertIn(timeout_record["lastSignal"], {"SIGTERM", "SIGKILL"})
            self.assertIn("Health check exceeded its 1000ms deadline.", result["timeoutOutput"]["output"])
        finally:
            if server:
                server.shutdown()
                server.server_close()
            self.restore_home(root, previous)
