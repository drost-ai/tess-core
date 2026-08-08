from __future__ import annotations

from tests.support.agent_core_case import *


class ShellCommandTests(AgentCoreTestCase):
    def test_dispatcher_covers_exactly_the_registered_builtins(self):
        from agent_core.shell_surface.dispatcher import _HANDLERS
        from agent_core.shell_surface.registry import listSlashCommands

        listed = {
            command["name"]
            for command in listSlashCommands(
                {"includeStatusline": True, "includeTheme": True, "includeMouse": True, "includeScreen": True}
            )
        }
        self.assertEqual(listed, set(_HANDLERS))

    def test_slash_command_registry_and_core_handlers(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            session["todos"] = [{"content": "ship shell slice", "status": "in_progress"}]
            session["metadata"]["compactionState"] = {
                "workingContextTokenEstimate": 4200,
                "workingContextTokenMeasurement": {"source": "openai_responses_estimate"},
                "compactedThroughMessageIndex": 3,
            }
            session["metadata"]["tokenTracking"] = {"lastPreflight": {"inputTokens": 300, "safetyMarginTokens": 30, "source": "openai_responses_estimate"}}
            session["metadata"]["fullTranscriptLog"] = {"archivedMessageCount": 5, "path": str(workspace / "archive.jsonl")}
            infos = []
            errors = []
            counters = {"persist": 0, "refresh": 0, "clear": 0}
            hooks = {
                "info": lambda title, message: infos.append((title, message)),
                "error": lambda message: errors.append(message),
                "persist": lambda: counters.__setitem__("persist", counters["persist"] + 1),
                "refresh": lambda: counters.__setitem__("refresh", counters["refresh"] + 1),
                "clear": lambda: counters.__setitem__("clear", counters["clear"] + 1),
                "getTheme": lambda: "tess",
                "setTheme": lambda _theme: None,
            }

            runtime_seen = {}

            def hello_handler(payload):
                runtime_seen["hello"] = payload
                return "hello from plugin"

            def status_override(payload):
                runtime_seen["status_override"] = payload
                return "plugin status"

            runtime_commands = [{"name": "hello", "usage": "/hello", "description": "Say hello", "handler": hello_handler}, {"name": "status", "usage": "/status", "description": "Should not override builtin", "handler": status_override}]
            commands = core.listSlashCommands({"includeTheme": True, "runtimeCommands": runtime_commands})
            names = [command["name"] for command in commands]
            self.assertIn("provider", names)
            self.assertIn("tasks", names)
            self.assertIn("theme", names)
            self.assertIn("hello", names)
            self.assertIn("transcript", names)
            self.assertIn("debug", names)
            self.assertIn("diagnostics", names)
            self.assertIn("collaboration", names)
            self.assertEqual(next(command for command in commands if command["name"] == "daemon")["usage"], "/daemon [restart]")

            help_result = run(core.handleSlashCommand({"command": "/help", "session": session, "config": config, "hooks": hooks, "runtimeCommands": runtime_commands}))
            self.assertEqual(help_result["outcome"], "handled")
            self.assertIn("/provider [name]", help_result["message"])
            self.assertIn("/agents inspect ID", help_result["message"])
            self.assertIn("/tasks retry ID", help_result["message"])
            self.assertIn("/memory append TEXT", help_result["message"])
            self.assertIn("/hello", help_result["message"])
            self.assertIn("/transcript", help_result["message"])

            self.assertEqual(run(core.handleSlashCommand({"command": "plain text"}))["outcome"], "pass")
            unknown = run(core.handleSlashCommand({"command": "/unknown", "session": session, "config": config, "hooks": hooks}))
            self.assertTrue(unknown["isError"])
            self.assertIn("Unknown slash command: /unknown", errors[-1])
            self.assertEqual(run(core.handleSlashCommand({"command": "/hello plugin args", "session": session, "config": config, "hooks": hooks, "runtimeCommands": runtime_commands}))["message"], "hello from plugin")
            self.assertEqual(runtime_seen["hello"]["argText"], "plugin args")
            self.assertEqual(runtime_seen["hello"]["args"], "plugin args")
            run(core.handleSlashCommand({"command": "/clear", "session": session, "config": config, "hooks": hooks}))
            self.assertEqual(counters["clear"], 1)

            status = run(core.handleSlashCommand({"command": "/status", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("session", status["message"])
            self.assertIn("working ctx  4200", status["message"])
            self.assertIn("ctx source   openai_responses_estimate", status["message"])
            self.assertIn("preflight    300 (+30) via openai_responses_estimate", status["message"])
            self.assertIn("compacted to 3", status["message"])
            self.assertIn("archived     5", status["message"])
            self.assertIn(f"archive log  {workspace / 'archive.jsonl'}", status["message"])
            self.assertNotIn("status_override", runtime_seen)
            self.assertEqual(infos[-1][0], "Status")
            providers_report = run(core.handleSlashCommand({"command": "/providers", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("openai  (active)", providers_report["message"])
            self.assertIn("mode       responses", providers_report["message"])
            self.assertIn("auth       openai_api_key via OPENAI_API_KEY", providers_report["message"])
            self.assertIn("about      Official OpenAI Responses API", providers_report["message"])

            run(core.handleSlashCommand({"command": "/model claude-opus-4-6", "session": session, "config": config, "hooks": hooks}))
            self.assertEqual(session["provider"], "anthropic")
            self.assertEqual(config["providerProfileName"], "anthropic-console")
            run(core.handleSlashCommand({"command": "/model openai:gpt-4.1", "session": session, "config": config, "hooks": hooks}))
            unsupported_effort = run(core.handleSlashCommand({"command": "/effort high", "session": session, "config": config, "hooks": hooks}))
            self.assertTrue(unsupported_effort["isError"])
            self.assertIn("does not support configurable reasoning effort", errors[-1])
            run(core.handleSlashCommand({"command": "/model openai:gpt", "session": session, "config": config, "hooks": hooks}))
            self.assertEqual(session["provider"], "openai")
            self.assertEqual(session["model"], "gpt-5.4")
            run(core.handleSlashCommand({"command": "/effort xhigh", "session": session, "config": config, "hooks": hooks}))
            self.assertEqual(session["effort"], "xhigh")
            permissions_report = run(core.handleSlashCommand({"command": "/permissions", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("modes    acceptEdits, bypassPermissions, default, dontAsk, auto", permissions_report["message"])
            run(core.handleSlashCommand({"command": "/permissions auto", "session": session, "config": config, "hooks": hooks}))
            self.assertEqual(session["permissionMode"], "auto")
            self.assertGreaterEqual(counters["persist"], 3)
            self.assertGreaterEqual(counters["refresh"], 3)

            policy = run(core.handleSlashCommand({"command": "/permissions policy", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("auto allow", policy["message"])
            run(core.handleSlashCommand({"command": "/permissions allow add python -m pytest", "session": session, "config": config, "hooks": hooks}))
            run(core.handleSlashCommand({"command": "/permissions deny add rm -rf build", "session": session, "config": config, "hooks": hooks}))
            auto_mode = run(core.loadAutoMode(str(workspace)))
            self.assertIn("python -m pytest", auto_mode["allow"])
            self.assertIn("rm -rf build", auto_mode["deny"])
            run(core.handleSlashCommand({"command": "/permissions allow remove python -m pytest", "session": session, "config": config, "hooks": hooks}))
            self.assertNotIn("python -m pytest", run(core.loadAutoMode(str(workspace)))["allow"])
            run(core.handleSlashCommand({"command": "/permissions reset", "session": session, "config": config, "hooks": hooks}))
            self.assertNotIn("rm -rf build", run(core.loadAutoMode(str(workspace)))["deny"])

            provider_result = run(core.handleSlashCommand({"command": "/provider anthropic-console", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("anthropic-console", provider_result["message"])
            self.assertEqual(session["provider"], "anthropic")
            self.assertEqual(config["providerProfileName"], "anthropic-console")
            bad_provider = run(core.handleSlashCommand({"command": "/provider missing-profile", "session": session, "config": config, "hooks": hooks}))
            self.assertTrue(bad_provider["isError"])
            self.assertIn("Unknown provider profile", errors[-1])

            todos = run(core.handleSlashCommand({"command": "/todos", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("ship shell slice", todos["message"])
            usage = run(core.handleSlashCommand({"command": "/cost", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("input tokens", usage["message"])

            run(core.saveChildAgent({"agentId": "agent-shell", "sessionId": "child-shell", "parentSessionId": session["id"], "name": "Shell worker", "provider": "openai", "model": "gpt-5.4", "mode": "auto", "runInBackground": True, "status": "backgrounded", "startedAt": "2026-01-01T00:00:00Z", "lastSummary": "working"}))
            session["metadata"]["childAgentIds"] = ["agent-shell"]
            agents = run(core.handleSlashCommand({"command": "/agents running", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("agent-shell", agents["message"])

            run(core.saveTaskRecord({"id": "task-shell", "sessionId": session["id"], "kind": "background_command", "status": "running", "title": "Shell task", "updatedAt": "2026-01-01T00:00:00Z"}))
            tasks = run(core.handleSlashCommand({"command": "/tasks", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("task-shell", tasks["message"])
            inspected = run(core.handleSlashCommand({"command": "/tasks inspect task-shell", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("kind         background_command", inspected["message"])
            self.assertIn("surface      inspectable", inspected["message"])

            run(core.saveDaemonState("shell-instance", {"phase": "running", "pid": 123, "socketPath": str(workspace / "daemon.sock"), "supervisorId": "sup", "epoch": 1, "attachedClients": 0, "watchedSessions": 1, "activeTasks": 1, "updatedAt": "2026-01-01T00:00:00Z"}))
            daemon = run(core.handleSlashCommand({"command": "/daemon", "session": session, "config": config, "hooks": hooks, "runtimeInstanceId": "shell-instance"}))
            self.assertIn("running", daemon["message"])

            exit_result = run(core.handleSlashCommand({"command": "/quit", "session": session, "config": config, "hooks": hooks}))
            self.assertEqual(exit_result["outcome"], "exit")
        finally:
            self.restore_home(root, previous)

    def test_shell_commands_memory_monitor_resume_compact_review_and_tui(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            infos = []
            errors = []
            events = []
            resumed = []
            prompts = []
            compaction_calls = []
            tui_state = {"statusline": True, "theme": "tess", "mouse": "selection", "screen": "auto", "persist": 0, "refresh": 0}

            def fake_compaction(payload):
                compaction_calls.append(payload)
                return {"status": "skipped", "reason": "below_threshold", "detail": "not enough transcript"}

            def fake_run_prompt(payload):
                prompts.append(payload)
                return {"text": "review queued", "stopReason": "stop"}

            hooks = {
                "info": lambda title, message: infos.append((title, message)),
                "error": lambda message: errors.append(message),
                "persist": lambda: tui_state.__setitem__("persist", tui_state["persist"] + 1),
                "refresh": lambda: tui_state.__setitem__("refresh", tui_state["refresh"] + 1),
                "emitRuntimeEvent": lambda event, data: events.append((event, data)),
                "getExtensionsReport": lambda: "mcp: ok",
                "requestResume": lambda next_session: resumed.append(next_session["id"]),
                "runCompaction": fake_compaction,
                "runPrompt": fake_run_prompt,
                "getStatusLineVisible": lambda: tui_state["statusline"],
                "setStatusLineVisible": lambda visible: tui_state.__setitem__("statusline", visible),
                "getTheme": lambda: tui_state["theme"],
                "setTheme": lambda theme: tui_state.__setitem__("theme", theme),
                "getMouseMode": lambda: tui_state["mouse"],
                "setMouseMode": lambda mode: tui_state.__setitem__("mouse", mode),
                "getScreenMode": lambda: tui_state["screen"],
                "setScreenMode": lambda mode: tui_state.__setitem__("screen", mode),
            }

            memory = run(core.handleSlashCommand({"command": "/memory set Prefer shell parity tests", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("Replaced project memory", memory["message"])
            run(core.handleSlashCommand({"command": "/memory append Keep handlers deterministic", "session": session, "config": config, "hooks": hooks}))
            memory_show = run(core.handleSlashCommand({"command": "/memory", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("Prefer shell parity tests", memory_show["message"])
            self.assertTrue(any(record["id"] == "manual-project-memory" for record in run(core.loadProjectMemoryRecords(str(workspace)))))

            monitor = core.createMonitorRecord({"sessionId": session["id"], "taskId": "task-shell-monitor", "goal": "Watch shell parity"})
            run(core.saveMonitor(monitor))
            monitors = run(core.handleSlashCommand({"command": "/monitors", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("Watch shell parity", monitors["message"])
            run(core.handleSlashCommand({"command": f"/monitor stop {monitor['id'][:8]}", "session": session, "config": config, "hooks": hooks}))
            self.assertEqual(run(core.loadMonitor(monitor["id"]))["status"], "stopped")
            run(core.handleSlashCommand({"command": f"/monitor resume {monitor['id']}", "session": session, "config": config, "hooks": hooks}))
            self.assertEqual(run(core.loadMonitor(monitor["id"]))["status"], "active")

            other = core.createEmptySession(cwd=str(workspace), provider="openai", providerProfileName=config["providerProfileName"], model="gpt-5.4", effort="medium", systemPrompt="s", permissionMode="default")
            other["messages"] = [{"role": "user", "content": "resume target session"}]
            run(core.saveSession(other))
            resume_list = run(core.handleSlashCommand({"command": "/resume", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("resume target session", resume_list["message"])
            run(core.handleSlashCommand({"command": f"/resume {other['id'][:8]}", "session": session, "config": config, "hooks": hooks}))
            self.assertEqual(resumed, [other["id"]])

            compact = run(core.handleSlashCommand({"command": "/compact", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("Checkpoint not created", compact["message"])
            self.assertEqual(compaction_calls[0]["trigger"], "manual")
            self.assertTrue(any(event == "compaction.started" for event, _ in events))
            self.assertTrue(any(event == "compaction.skipped" for event, _ in events))

            review = run(core.handleSlashCommand({"command": "/review providers", "session": session, "config": config, "hooks": hooks}))
            self.assertEqual(review["message"], "review queued")
            self.assertEqual(prompts[0]["agentName"], "reviewer")
            self.assertIn("providers", prompts[0]["prompt"])

            extensions = run(core.handleSlashCommand({"command": "/extensions", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("mcp: ok", extensions["message"])
            run(core.handleSlashCommand({"command": "/statusline off", "session": session, "config": config, "hooks": hooks}))
            run(core.handleSlashCommand({"command": "/theme next", "session": session, "config": config, "hooks": hooks}))
            mouse_status = run(core.handleSlashCommand({"command": "/mouse", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("Native terminal selection is enabled", mouse_status["message"])
            mouse_toggle = run(core.handleSlashCommand({"command": "/mouse toggle", "session": session, "config": config, "hooks": hooks}))
            self.assertIn("Mouse capture on", mouse_toggle["message"])
            run(core.handleSlashCommand({"command": "/screen inline", "session": session, "config": config, "hooks": hooks}))
            self.assertEqual(tui_state["statusline"], False)
            self.assertEqual(tui_state["theme"], "copper")
            self.assertEqual(tui_state["mouse"], "capture")
            self.assertEqual(tui_state["screen"], "inline")
            self.assertGreaterEqual(tui_state["persist"], 3)
            self.assertGreaterEqual(tui_state["refresh"], 2)
        finally:
            self.restore_home(root, previous)

    def test_shell_task_and_daemon_mutation_commands(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            target = core.createEmptySession(cwd=str(workspace), provider="openai", providerProfileName=config["providerProfileName"], model="gpt-5.4", effort="medium", systemPrompt="s", permissionMode="default")
            run(core.saveSession(target))
            task = {"id": "task-shell-mutate", "sessionId": session["id"], "kind": "background_command", "status": "running", "title": "Mutable shell task", "updatedAt": "2026-01-01T00:00:00Z", "attachableSessionId": target["id"]}
            run(core.saveTaskRecord(task))
            stopped = []
            retried = []
            daemon_actions = []
            resumed = []
            counters = {"persist": 0}
            hooks = {
                "info": lambda *_: None,
                "error": lambda *_: None,
                "persist": lambda: counters.__setitem__("persist", counters["persist"] + 1),
                "requestResume": lambda next_session: resumed.append(next_session["id"]),
            }
            supervisor = {
                "readTaskOutput": lambda task_id, _tail: {"output": f"log for {task_id}"},
                "stopTask": lambda task_id: stopped.append(task_id) or True,
                "retryTask": lambda task_id: retried.append(task_id) or {"taskId": "task-shell-retry", "outputFile": str(workspace / "retry.log")},
            }
            daemon_controller = {
                "restart": lambda: daemon_actions.append("restart"),
            }

            log = run(core.handleSlashCommand({"command": "/tasks log mutate", "session": session, "config": config, "hooks": hooks, "taskSupervisor": supervisor}))
            self.assertIn("log for task-shell-mutate", log["message"])
            stop = run(core.handleSlashCommand({"command": "/tasks stop task-shell", "session": session, "config": config, "hooks": hooks, "taskSupervisor": supervisor}))
            self.assertIn("Stopped task-shell-mutate", stop["message"])
            retry = run(core.handleSlashCommand({"command": "/tasks retry mutate", "session": session, "config": config, "hooks": hooks, "taskSupervisor": supervisor}))
            self.assertIn("new task     task-shell-retry", retry["message"])
            run(core.handleSlashCommand({"command": "/tasks attach mutate", "session": session, "config": config, "hooks": hooks, "taskSupervisor": supervisor}))
            self.assertEqual(stopped, ["task-shell-mutate"])
            self.assertEqual(retried, ["task-shell-mutate"])
            self.assertEqual(resumed, [target["id"]])
            self.assertEqual(counters["persist"], 1)

            stopped_daemon = run(core.handleSlashCommand({"command": "/daemon stop", "session": session, "config": config, "hooks": hooks, "daemonController": daemon_controller, "runtimeInstanceId": "shell-instance"}))
            restarted_daemon = run(core.handleSlashCommand({"command": "/daemon restart", "session": session, "config": config, "hooks": hooks, "daemonController": daemon_controller, "runtimeInstanceId": "shell-instance"}))
            self.assertEqual(stopped_daemon["message"], "Daemon not running.")
            self.assertEqual(restarted_daemon["message"], "Daemon not running.")
            self.assertEqual(daemon_actions, ["restart"])
        finally:
            self.restore_home(root, previous)

    def test_shell_availability_settings_and_inherited_permissions(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            hooks = {"persist": lambda: None, "refresh": lambda: None}
            availability = core.getSlashCommandAvailability({"mode": "bare", "hooks": hooks})
            review = next(command for command in availability if command["name"] == "review")
            theme = next(command for command in availability if command["name"] == "theme")
            self.assertFalse(review["available"])
            self.assertEqual(review["reason"], "interactive runtime required")
            self.assertFalse(theme["available"])
            self.assertEqual(theme["reason"], "full-screen TUI required")

            run(core.handleSlashCommand({"command": "/provider anthropic-console", "session": session, "config": config, "hooks": hooks}))
            run(core.handleSlashCommand({"command": "/model openai:gpt-5.4-mini", "session": session, "config": config, "hooks": hooks}))
            run(core.handleSlashCommand({"command": "/effort high", "session": session, "config": config, "hooks": hooks}))
            run(core.handleSlashCommand({"command": "/permissions acceptEdits", "session": session, "config": config, "hooks": hooks}))
            settings = run(core.loadSettings("project", str(workspace)))
            self.assertEqual(settings["provider"], "openai")
            self.assertEqual(settings["model"], "gpt-5.4-mini")
            self.assertEqual(settings["effortLevel"], "high")
            self.assertEqual(settings["permissionMode"], "acceptEdits")

            parent = core.createEmptySession(cwd=str(workspace), provider="openai", providerProfileName=config["providerProfileName"], model="gpt-5.4", effort="medium", systemPrompt="s", permissionMode="bypassPermissions")
            run(core.saveSession(parent))
            child = core.createEmptySession(cwd=str(workspace), provider="openai", providerProfileName=config["providerProfileName"], model="gpt-5.4", effort="medium", systemPrompt="s", permissionMode="default")
            child["metadata"]["parentSessionId"] = parent["id"]
            report = run(core.handleSlashCommand({"command": "/permissions", "session": child, "config": config, "hooks": hooks}))
            self.assertIn("current  default", report["message"])
            self.assertIn(f"inherited bypassPermissions from {parent['id'][:12]}", report["message"])
            self.assertIn("effective bypassPermissions", report["message"])
        finally:
            self.restore_home(root, previous)
