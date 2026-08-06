from __future__ import annotations

from tests.support.agent_core_case import *


class ToolContractTests(AgentCoreTestCase):
    def test_builtin_tool_contract_snapshot_matches_reference_fixture(self):
        expected = load_builtin_tool_contract_fixture()
        snapshot = core.createBuiltinToolContractSnapshot()
        self.assertEqual(snapshot, expected)
        self.assertEqual([item["name"] for item in snapshot], [tool.name for tool in core.createBuiltinTools()])

    def test_builtin_tool_schemas_are_cloned_per_definition(self):
        first_read = next(tool for tool in core.createBuiltinTools() if tool.name == "Read")
        first_read.inputSchema["properties"]["file_path"]["type"] = "mutated"
        second_read = next(tool for tool in core.createBuiltinTools() if tool.name == "Read")
        self.assertEqual(second_read.inputSchema["properties"]["file_path"]["type"], "string")
        self.assertEqual(core.BUILTIN_TOOL_INPUT_SCHEMAS["Read"]["properties"]["file_path"]["type"], "string")

    def test_builtin_tool_schemas_are_provider_strict(self):
        for tool in core.createBuiltinTools():
            self.assertIs(tool.inputSchema.get("additionalProperties"), False, tool.name)
        grep = next(tool for tool in core.createBuiltinTools() if tool.name == "Grep")
        self.assertEqual(grep.inputSchema["properties"]["output_mode"]["enum"], ["files_with_matches", "content", "count"])
        agent = next(tool for tool in core.createBuiltinTools() if tool.name == "Agent")
        self.assertEqual(agent.inputSchema["properties"]["isolation"]["enum"], ["shared", "worktree", "none"])

    def test_builtin_tool_behavior_parity_for_questions_status_config_todo_and_mcp(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            config["mcpConfig"] = [{"name": "edge", "transport": "http", "commandOrUrl": "https://mcp.example.test"}]
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            events = []
            prompts = []
            context = {"session": session, "config": config, "interactive": True, "askQuestion": lambda question, options: prompts.append((question, options)) or "Blue", "outputJson": lambda event, data: events.append((event, data)), "cwd": str(workspace), "persistSession": lambda: None}
            tools = {tool.name: tool for tool in core.createBuiltinTools()}

            blocked = run(tools["AskUserQuestion"].handler({"questions": [{"question": "Pick?", "options": [{"label": "Blue"}]}]}, {**context, "interactive": False}))
            self.assertTrue(blocked["isError"])
            self.assertIn("print mode", blocked["content"])

            asked = run(tools["AskUserQuestion"].handler({"questions": [{"question": "Pick?", "options": [{"label": "Blue"}, {"label": "Red"}]}]}, context))
            self.assertEqual(json.loads(asked["content"]), {"answers": [{"question": "Pick?", "answer": "Blue"}]})
            self.assertEqual(prompts, [("Pick?", ["Blue", "Red"])])

            status = run(tools["SetStatus"].handler({"message": "Working", "detail": "tools"}, context))
            self.assertEqual(json.loads(status["content"]), {"ok": True, "message": "Working", "detail": "tools"})
            self.assertEqual(events[-1], ("status.updated", {"message": "Working", "detail": "tools", "session_id": session["id"]}))

            todo = run(tools["TodoWrite"].handler({"todos": [{"content": "Ship parity", "status": "pending"}]}, context))
            self.assertEqual(json.loads(todo["content"])["todos"][0]["activeForm"], "ship")

            config_payload = json.loads(run(tools["Config"].handler({}, context))["content"])
            self.assertEqual(config_payload["cwd"], str(workspace))
            self.assertEqual(config_payload["providerProfile"], config["providerProfileName"])
            self.assertEqual(config_payload["providerKind"], "openai")
            self.assertEqual(config_payload["tools"], config["tools"])
            self.assertEqual(config_payload["allowedRoots"], config["allowedRoots"])
            self.assertEqual(config_payload["mcpServers"], ["edge"])

            mcp_payload = json.loads(run(tools["Mcp"].handler({}, context))["content"])
            self.assertEqual(mcp_payload["servers"][0]["capabilities"], {"tools": 0, "resources": 0, "channelsSupported": False})
            self.assertEqual(mcp_payload["servers"][0]["channelRegistration"], {"requested": False, "status": "not_requested"})
        finally:
            self.restore_home(root, previous)

    def test_tool_payloads_omit_undefined_fields_and_preserve_reference_line_splitting(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            text_file = workspace / "lines.txt"
            text_file.write_text("one\ntwo\n", encoding="utf-8")
            read = next(tool for tool in core.createBuiltinTools() if tool.name == "Read")
            context = {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None}

            payload = json.loads(run(read.handler({"file_path": str(text_file)}, context))["content"])
            file_payload = payload["file"]
            self.assertEqual(file_payload["content"], "one\ntwo\n")
            self.assertEqual(file_payload["totalLines"], 3)
            self.assertEqual(file_payload["transcriptView"]["originalLines"], 3)
            self.assertNotIn("artifact", file_payload)
            self.assertNotIn("nextOffset", file_payload)

            shaped = run(core.shapeTextToolResult(config=config, sessionId=session["id"], toolName="Bash", family="bash", text="alpha\n"))
            self.assertEqual(shaped["view"]["originalLines"], 2)
            self.assertEqual(shaped["view"]["returnedLines"], 2)
            self.assertNotIn("artifact", shaped["view"])
        finally:
            self.restore_home(root, previous)

    def test_file_change_payload_diff_matches_reference_shape(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            tools = {tool.name: tool for tool in core.createBuiltinTools()}
            context = {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None}

            created = json.loads(run(tools["Write"].handler({"file_path": "diff.txt", "content": "one\ntwo\nthree\nfour\nfive\n"}, context))["content"])
            self.assertNotIn("note", created)

            edited = json.loads(run(tools["Edit"].handler({"file_path": "diff.txt", "old_string": "two", "new_string": "TWO"}, context))["content"])
            self.assertEqual(
                edited["diff"],
                "\n".join(
                    [
                        "  1   one",
                        "  2 - two",
                        "  2 + TWO",
                        "  3   three",
                        "  4   four",
                        "  5   five",
                    ]
                ),
            )
            self.assertNotIn("---", edited["diff"])
            self.assertEqual(edited["note"], "Replaced the first matching occurrence.")
        finally:
            self.restore_home(root, previous)
