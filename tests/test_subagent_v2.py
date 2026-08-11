from __future__ import annotations

from tests.support.agent_core_case import *


class SubagentV2Tests(AgentCoreTestCase):
    def test_agent_runtime_contract_persists_and_validates_structured_foreground_result(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "acceptEdits"}, str(workspace)))
            config["tools"] = ["Read", "Grep", "Agent"]
            config["agents"] = {
                "researcher": {
                    "prompt": "Research only.",
                    "tools": ["Read", "Grep"],
                    "maxToolCalls": 3,
                    "responseSchema": {
                        "type": "object",
                        "required": ["summary"],
                        "properties": {"summary": {"type": "string"}},
                        "additionalProperties": False,
                    },
                    "communication": {"allowChildQuestions": True, "maxPendingQuestions": 2},
                    "concurrencyGroup": "research",
                }
            }
            parent = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="acceptEdits")
            from agent_core.subagents import prepareSubagent

            context = {
                "session": parent,
                "config": config,
                "interactive": False,
                "askQuestion": lambda *_: "",
                "outputJson": lambda *_: None,
                "cwd": str(workspace),
                "persist_session": lambda: None,
            }
            prepared = run(prepareSubagent({
                "context": context,
                "description": "Investigate",
                "prompt": "Find it",
                "agentType": "researcher",
                "runInBackground": True,
            }))
            contract = prepared["childSession"]["metadata"]["subagentRuntimeContract"]
            self.assertEqual(contract["tools"], ["Read", "Grep"])
            self.assertEqual(contract["maxToolCalls"], 3)
            self.assertEqual(contract["communication"]["maxPendingQuestions"], 2)
            self.assertEqual(contract["concurrencyGroup"], "research")
            self.assertEqual(prepared["runtimeContractSummary"]["tools"], ["Read", "Grep"])
            child_record = run(core.load_child_agent(prepared["agentId"]))
            self.assertEqual(child_record["runtimeContract"]["tools"], ["Read", "Grep"])

            # Structured validation now runs in the background finalizer.
            os.environ["TESS_CHILD_AGENT_ID"] = prepared["agentId"]
            os.environ["TESS_CHILD_TASK_ID"] = "structured-task"
            try:
                run(core.finalizeBackgroundSubagentSuccessFromEnv(
                    prepared["childSession"],
                    {"text": '{"summary":"done"}', "toolCount": 0, "usage": {}, "stopReason": "end_turn"},
                ))
            finally:
                os.environ.pop("TESS_CHILD_AGENT_ID", None)
                os.environ.pop("TESS_CHILD_TASK_ID", None)
            child_record = run(core.load_child_agent(prepared["agentId"]))
            self.assertEqual(child_record["structuredResultStatus"], "valid")
            self.assertEqual(child_record["structuredResult"], {"summary": "done"})
        finally:
            self.restore_home(root, previous)

    def test_structured_result_unsupported_schema_keywords_fail_closed(self):
        evaluation = core.evaluate_subagent_result(
            '{"summary":"done"}',
            {
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string", "pattern": "^done$"}},
                "additionalProperties": False,
            },
        )
        self.assertEqual(evaluation["status"], "unsupported_schema")
        self.assertIsNone(evaluation["data"])
        self.assertEqual(evaluation["errors"], ["$.properties.summary: unsupported schema keyword 'pattern'"])

    def test_run_agent_prompt_filters_subagent_tools_before_dispatch(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            config["sessionPersistence"] = False
            config["tools"] = ["AllowedTool", "BlockedTool"]
            config["allowedTools"] = ["AllowedTool", "BlockedTool"]
            session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            session["metadata"]["subagentRuntimeContract"] = {"tools": ["AllowedTool"]}
            blocked_called = {"value": False}

            async def allowed(_input, _context):
                return {"content": "allowed"}

            async def blocked(_input, _context):
                blocked_called["value"] = True
                return {"content": "blocked"}

            tools = [
                core.ToolDefinition("AllowedTool", "Allowed", {"type": "object", "properties": {}}, allowed),
                core.ToolDefinition("BlockedTool", "Blocked", {"type": "object", "properties": {}}, blocked),
            ]
            calls = {"count": 0}

            async def send(_request):
                calls["count"] += 1
                if calls["count"] == 1:
                    return {
                        "model": config["model"],
                        "content": [{"type": "tool_use", "id": "tool-1", "name": "BlockedTool", "input": {}}],
                        "usage": {},
                        "stopReason": "tool_use",
                    }
                return {
                    "model": config["model"],
                    "content": [{"type": "text", "text": "finished"}],
                    "usage": {},
                    "stopReason": "end_turn",
                }

            result = run(
                core.runAgentPrompt(
                    {
                        "session": session,
                        "config": config,
                        "prompt": "run",
                        "runtime": bind_test_runtime(session, {"tools": tools, "interactive": False, "emitEvent": lambda *_: None}),
                        "providerOverride": {"send": send},
                    }
                )
            )
            self.assertFalse(blocked_called["value"])
            self.assertEqual(result["text"], "finished")
            tool_result_message = session["messages"][2]
            self.assertIn("Tool not available: BlockedTool", tool_result_message["content"][0]["content"])
        finally:
            self.restore_home(root, previous)

    def test_run_agent_prompt_enforces_subagent_max_tool_calls_after_allowed_call(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            config["sessionPersistence"] = False
            config["tools"] = ["AllowedTool"]
            config["allowedTools"] = ["AllowedTool"]
            session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            session["metadata"]["subagentRuntimeContract"] = {"tools": ["AllowedTool"], "maxToolCalls": 1}
            calls = {"tool": 0, "provider": 0}

            async def allowed(_input, _context):
                calls["tool"] += 1
                return {"content": "allowed"}

            async def send(_request):
                calls["provider"] += 1
                if calls["provider"] == 1:
                    return {
                        "model": config["model"],
                        "content": [
                            {"type": "tool_use", "id": "tool-1", "name": "AllowedTool", "input": {}},
                            {"type": "tool_use", "id": "tool-2", "name": "AllowedTool", "input": {}},
                        ],
                        "usage": {},
                        "stopReason": "tool_use",
                    }
                return {
                    "model": config["model"],
                    "content": [{"type": "text", "text": "finished"}],
                    "usage": {},
                    "stopReason": "end_turn",
                }

            result = run(
                core.runAgentPrompt(
                    {
                        "session": session,
                        "config": config,
                        "prompt": "run",
                        "runtime": bind_test_runtime(session, {"tools": [core.ToolDefinition("AllowedTool", "Allowed", {"type": "object", "properties": {}}, allowed)], "interactive": False, "emitEvent": lambda *_: None}),
                        "providerOverride": {"send": send},
                    }
                )
            )
            self.assertEqual(calls["tool"], 1)
            # The limit ends the turn at the next loop top instead of buying
            # further provider calls (TS agent-core parity).
            self.assertEqual(calls["provider"], 1)
            self.assertEqual(result["stopReason"], "max_tool_calls")
            tool_results = session["messages"][2]["content"]
            self.assertFalse(tool_results[0]["is_error"])
            self.assertTrue(tool_results[1]["is_error"])
            self.assertIn("max_tool_calls", tool_results[1]["content"])
        finally:
            self.restore_home(root, previous)

    def test_task_mailbox_delivers_once_and_records_events(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            session["metadata"]["subagentTaskId"] = "task-mailbox"
            run(core.write_task_meta("task-mailbox", {"type": "subagent", "task_id": "task-mailbox", "session_id": session["id"], "status": "running", "startedAt": session["createdAt"], "outputFile": core.task_output_path("task-mailbox")}))
            message = run(core.send_task_message("task-mailbox", {"childSessionId": session["id"], "content": "Use the new clue."}))
            self.assertEqual(message["status"], "queued")
            delivered = run(core.deliver_queued_task_messages_to_session(session))
            self.assertEqual([item["messageId"] for item in delivered], [message["messageId"]])
            self.assertIn("[PARENT MESSAGE]", session["messages"][-1]["content"])
            self.assertEqual(run(core.read_undelivered_task_messages("task-mailbox")), [])
            events = run(core.read_task_events("task-mailbox"))
            self.assertEqual([event["type"] for event in events], ["message.queued", "message.delivered"])
        finally:
            self.restore_home(root, previous)

    def test_mailbox_and_question_record_shapes_are_pinned(self):
        # Characterization pins for audit B6: these capture current durable shapes,
        # not a claim that the final API contract is settled.
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            run(core.write_task_meta("task-contract-pin", {"type": "subagent", "task_id": "task-contract-pin", "session_id": session["id"], "status": "running", "startedAt": "2026-07-05T00:00:00Z", "outputFile": core.task_output_path("task-contract-pin")}))
            message = run(
                core.send_task_message(
                    "task-contract-pin",
                    {
                        "messageId": "msg-contract-pin",
                        "childSessionId": session["id"],
                        "sender": "supervisor",
                        "kind": "answer",
                        "content": "Use the verified path.",
                        "createdAt": "2026-07-05T00:00:01Z",
                        "metadata": {"questionId": "q-contract-pin"},
                    },
                )
            )
            question = run(
                core.create_task_question(
                    "task-contract-pin",
                    {
                        "questionId": "q-contract-pin",
                        "childSessionId": session["id"],
                        "question": "Which path should I edit?",
                        "options": ["A", "B"],
                        "createdAt": "2026-07-05T00:00:02Z",
                        "metadata": {"source": "test"},
                    },
                )
            )
            self.assertEqual(
                message,
                {
                    "messageId": "msg-contract-pin",
                    "taskId": "task-contract-pin",
                    "direction": "to_child",
                    "childSessionId": session["id"],
                    "sender": "supervisor",
                    "kind": "answer",
                    "content": "Use the verified path.",
                    "status": "queued",
                    "createdAt": "2026-07-05T00:00:01Z",
                    "deliveredAt": None,
                    "metadata": {"questionId": "q-contract-pin"},
                },
            )
            self.assertEqual(
                question,
                {
                    "questionId": "q-contract-pin",
                    "taskId": "task-contract-pin",
                    "childSessionId": session["id"],
                    "question": "Which path should I edit?",
                    "options": ["A", "B"],
                    "status": "pending",
                    "createdAt": "2026-07-05T00:00:02Z",
                    "answeredAt": None,
                    "answer": None,
                    "metadata": {"source": "test"},
                },
            )
        finally:
            self.restore_home(root, previous)

    def test_ask_user_question_creates_durable_child_question_when_allowed(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            session["metadata"]["subagentTaskId"] = "task-question"
            session["metadata"]["subagentRuntimeContract"] = {"communication": {"allowChildQuestions": True, "maxPendingQuestions": 1}}
            run(core.write_task_meta("task-question", {"type": "subagent", "task_id": "task-question", "session_id": session["id"], "status": "running", "startedAt": session["createdAt"], "outputFile": core.task_output_path("task-question")}))
            question_tool = next(tool for tool in core.createBuiltinTools() if tool.name == "AskUserQuestion")
            result = run(
                question_tool.handler(
                    {"questions": [{"question": "Which path?", "options": [{"label": "A"}, {"label": "B"}]}]},
                    {"session": session, "config": config, "interactive": False, "persist_session": lambda: None},
                )
            )
            payload = json.loads(result["content"])
            self.assertEqual(payload["status"], "awaiting_parent")
            self.assertEqual(payload["questions"][0]["options"], ["A", "B"])
            questions = run(core.read_task_questions("task-question"))
            self.assertEqual(questions[0]["status"], "pending")
            meta = run(core.read_task_meta("task-question"))
            self.assertEqual(meta["status"], "awaiting_input")
            answered = run(core.answer_task_question("task-question", questions[0]["questionId"], "Use A"))
            self.assertEqual(answered["status"], "answered")
            self.assertEqual(len(run(core.read_undelivered_task_messages("task-question"))), 1)
        finally:
            self.restore_home(root, previous)

    def test_subagent_capacity_helper_respects_default_and_group_limits(self):
        records = [
            {"kind": "subagent", "status": "running", "metadata": {"concurrency_group": "research"}},
            {"kind": "subagent", "status": "backgrounded", "metadata": {"concurrency_group": "write"}},
        ]
        self.assertEqual(core.check_subagent_capacity({"maxConcurrency": 2, "groupLimits": {}}, records)["reason"], "max_concurrency")
        self.assertEqual(
            core.check_subagent_capacity({"maxConcurrency": 10, "groupLimits": {"research": 1}}, records, {"concurrencyGroup": "research"})["reason"],
            "concurrency_group",
        )
        self.assertIsNone(core.check_subagent_capacity({"maxConcurrency": 10, "groupLimits": {"research": 2}}, records, {"concurrencyGroup": "research"}))

    def test_background_finalizer_persists_structured_result_and_task_event(self):
        root, home, workspace, previous = self.temp_context()
        previous_env = {name: os.environ.get(name) for name in ("TESS_CHILD_AGENT_ID", "TESS_CHILD_TASK_ID", "TESS_CHILD_SESSION_ID")}
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            session["metadata"]["parentSessionId"] = "parent-bg"
            session["metadata"]["subagentRuntimeContract"] = {
                "responseSchema": {
                    "type": "object",
                    "required": ["ok"],
                    "properties": {"ok": {"type": "boolean"}},
                    "additionalProperties": False,
                }
            }
            os.environ["TESS_CHILD_AGENT_ID"] = "agent-bg-structured"
            os.environ["TESS_CHILD_TASK_ID"] = "task-bg-structured"
            os.environ["TESS_CHILD_SESSION_ID"] = session["id"]
            run(core.finalizeBackgroundSubagentSuccessFromEnv(session, {"text": '{"ok": true}', "stopReason": "end_turn"}))
            child = run(core.load_child_agent("agent-bg-structured"))
            self.assertEqual(child["structuredResultStatus"], "valid")
            self.assertEqual(child["structuredResult"], {"ok": True})
            meta = run(core.read_task_meta("task-bg-structured"))
            self.assertEqual(meta["structuredResultStatus"], "valid")
            self.assertTrue(Path(meta["structuredResultPath"]).exists())
            events = run(core.read_task_events("task-bg-structured"))
            self.assertEqual(events[-1]["type"], "task.subagent.completed")
            self.assertEqual(events[-1]["structuredResultStatus"], "valid")
        finally:
            for name, value in previous_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            self.restore_home(root, previous)
