from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core.tasks.communication import read_task_events, read_task_mailbox_messages, send_task_message


class TraceEmitterTests(AgentCoreTestCase):
    def test_worker_events_land_as_compact_trace_records(self):
        from agent_core.tasks.runners.subagent import _create_trace_emitter

        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise():
                await core.write_task_meta("trace-task", {
                    "type": "subagent", "taskId": "trace-task", "id": "trace-task",
                    "sessionId": "trace-child", "session_id": "trace-child",
                    "parent_session_id": "trace-parent", "status": "running",
                    "cwd": str(workspace), "startedAt": "2026-01-01T00:00:00Z",
                    "updatedAt": "2026-01-01T00:00:00Z",
                })
                emit = _create_trace_emitter("trace-task")
                emit("tool.started", {"name": "Bash", "tool_use_id": "t1", "input": {"command": "ls " + "x" * 500}})
                emit("tool.completed", {"name": "Bash", "tool_use_id": "t1", "output": "some output", "error": False})
                emit("message.assistant", {"text": "  I looked at\nthe files  ", "model": "m"})
                emit("turn.cancelled", {"session_id": "trace-child"})  # not traced
                await asyncio.gather(*[t for t in asyncio.all_tasks() if t is not asyncio.current_task()])
                return await read_task_events("trace-task")

            events = run(exercise())
            traces = [e for e in events if e.get("type") == "trace"]
            self.assertEqual([e["event"] for e in traces], ["tool.started", "tool.completed", "message.assistant"])
            started, completed, spoke = traces
            self.assertEqual(started["tool"], "Bash")
            self.assertLessEqual(len(started["input_preview"]), 160)
            self.assertFalse(completed["error"])
            self.assertEqual(completed["output_chars"], len("some output"))
            self.assertEqual(spoke["text_preview"], "I looked at the files")
        finally:
            self.restore_home(root, previous)

    def test_initial_resident_turn_marks_running_before_the_first_prompt(self):
        import agent_core.tasks.runners.subagent as runner_module

        calls = []

        async def fake_prepare(_args):
            calls.append(("prepare",))
            return {"id": "s"}, {}, {}

        async def fake_prompt(_values):
            calls.append(("prompt",))
            return {"text": "done", "stopReason": "end_turn"}

        async def fake_save(_session):
            return None

        async def fake_mark(_task_id, _agent_id, status, waiting_reason, summary=None):
            calls.append(("mark", status, waiting_reason))

        async def fake_publish(_task_id, _agent_id, _session, _result, *, trigger_ids):
            calls.append(("publish", trigger_ids))
            raise asyncio.CancelledError  # initial turn observed; stop the resident loop

        async def exercise():
            with mock.patch.object(runner_module, "_prepare_worker", fake_prepare), \
                    mock.patch.object(runner_module, "run_agent_prompt", fake_prompt), \
                    mock.patch.object(runner_module, "save_session", fake_save), \
                    mock.patch.object(runner_module, "_mark_resident_status", fake_mark), \
                    mock.patch.object(runner_module, "_publish_resident_turn_result", fake_publish):
                with self.assertRaises(asyncio.CancelledError):
                    await runner_module._run_resident_subagent({"taskId": "t", "agentId": "a", "sessionId": "s"})

        run(exercise())
        self.assertEqual(calls, [("prepare",), ("mark", "running", None), ("prompt",), ("publish", [])])


class ResidentSubagentTests(AgentCoreTestCase):
    def _seed_family(self, home, workspace):
        settings_path = home / "settings.json"
        settings_path.write_text(
            json.dumps({
                "provider": "resident-openai",
                "providerProfiles": {
                    "resident-openai": {
                        "kind": "openai",
                        "defaultModel": "resident-model",
                        "auth": {"kind": "openai_api_key", "envVar": "AGENT_CORE_TEST_MISSING_RESIDENT_KEY"},
                    }
                },
            }),
            encoding="utf-8",
        )
        os.environ.pop("AGENT_CORE_TEST_MISSING_RESIDENT_KEY", None)
        config = run(core.resolve_config(
            {"provider": "resident-openai", "settings": str(settings_path), "runtimeHome": str(home), "permissionMode": "bypassPermissions"},
            str(workspace),
        ))
        parent = core.create_empty_session(
            cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"],
            model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"],
            permissionMode="bypassPermissions",
        )
        child = core.create_empty_session(
            cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"],
            model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"],
            permissionMode="bypassPermissions",
        )
        child["messages"].append({"role": "user", "content": "report in"})
        child["metadata"]["parentSessionId"] = parent["id"]
        child["metadata"]["agentId"] = "resident-agent"
        child["metadata"]["agentCoreSettingsPath"] = str(settings_path)
        child_authority = run(core.capture_agent_authority(config=config, session=child, tools=core.create_builtin_tools()))
        child["metadata"]["effectiveAgentAuthority"] = child_authority
        run(core.save_session(parent))
        run(core.save_session(child))
        run(core.save_child_agent({
            "agentId": "resident-agent", "sessionId": child["id"], "parentSessionId": parent["id"],
            "name": "Resident", "provider": config["provider"], "model": config["model"],
            "mode": "bypassPermissions", "runInBackground": True, "status": "backgrounded",
            "startedAt": "2026-01-01T00:00:00Z", "effectiveAgentAuthority": child_authority,
        }))
        return config, parent, child

    def _wait_for(self, predicate, timeout=30.0, label=""):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        raise AssertionError(f"timeout waiting for {label}")

    def test_resident_child_persists_and_runs_continuation_turns(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        task_id = None
        try:
            os.environ["AGENT_CORE_SUBAGENTS_RESIDENT_POLL_MS"] = "200"
            config, parent, child = self._seed_family(home, workspace)
            supervisor = run(core.create_task_supervisor({"cwd": str(workspace)}))
            started = run(supervisor["startSubagentTask"]({
                "parentSessionId": parent["id"], "sessionId": child["id"],
                "agentId": "resident-agent", "cwd": str(workspace), "label": "Resident",
            }))
            task_id = started["taskId"]

            meta = self._wait_for(
                lambda: (lambda m: m if m and m.get("status") == "awaiting_input" else None)(run(core.read_task_meta(task_id))),
                label="resident idle after initial turn",
            )
            self.assertEqual(meta["waitingReason"], "resident_idle")
            child_record = run(core.load_child_agent("resident-agent"))
            self.assertEqual(child_record["status"], "awaiting_input")
            self.assertTrue(child_record["stoppable"])
            self.assertIsNone(child_record.get("completedAt"))
            task_record = run(core.load_task_record(task_id))
            self.assertEqual(task_record["status"], "awaiting_input")
            self.assertEqual(task_record["detailStatus"], "resident_idle")

            results = [m for m in run(read_task_mailbox_messages(task_id)) if m.get("kind") == "result" and m.get("direction") == "to_parent"]
            self.assertEqual(len(results), 1)
            self.assertIn("provider profile 'resident-openai'", results[0]["content"])

            baseline_messages = len(run(core.load_session(child["id"]))["messages"])
            run(send_task_message(task_id, {"direction": "to_child", "content": "follow up please"}))
            self._wait_for(
                lambda: len([m for m in run(read_task_mailbox_messages(task_id)) if m.get("kind") == "result" and m.get("direction") == "to_parent"]) >= 2,
                label="second resident turn result",
            )
            meta = self._wait_for(
                lambda: (lambda m: m if m and m.get("status") == "awaiting_input" else None)(run(core.read_task_meta(task_id))),
                label="idle again after continuation",
            )
            persisted_child = run(core.load_session(child["id"]))
            self.assertGreater(len(persisted_child["messages"]), baseline_messages)

            stopped = run(supervisor["stopTask"](task_id))
            self.assertEqual(stopped["status"], "stopped")
        finally:
            os.environ.pop("AGENT_CORE_SUBAGENTS_RESIDENT_POLL_MS", None)
            if supervisor:
                run(supervisor["close"]())
            self.restore_home(root, previous)


if __name__ == "__main__":
    unittest.main()


class SubagentCapacityGovernanceTests(AgentCoreTestCase):
    def test_capacity_roster_then_stop_frees_a_slot(self):
        root, home, workspace, previous = self.temp_context()
        supervisor = None
        try:
            os.environ["AGENT_CORE_SUBAGENTS_MAX_LIVE"] = "1"
            os.environ["AGENT_CORE_SUBAGENTS_RESIDENT_POLL_MS"] = "200"
            seeder = ResidentSubagentTests()
            config, parent, child = seeder._seed_family(home, workspace)
            events = []
            supervisor = run(core.create_task_supervisor({"cwd": str(workspace), "onEvent": lambda event: events.append(event)}))
            started = run(supervisor["startSubagentTask"]({
                "parentSessionId": parent["id"], "sessionId": child["id"],
                "agentId": "resident-agent", "cwd": str(workspace), "label": "Resident",
            }))
            task_id = started["taskId"]
            seeder._wait_for(
                lambda: (run(core.read_task_meta(task_id)) or {}).get("status") == "awaiting_input",
                label="first child resident idle",
            )

            second_child = core.create_empty_session(
                cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"],
                model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"],
                permissionMode="bypassPermissions",
            )
            second_child["metadata"]["parentSessionId"] = parent["id"]
            run(core.save_session(second_child))
            refused = run(supervisor["startSubagentTask"]({
                "parentSessionId": parent["id"], "sessionId": second_child["id"],
                "agentId": "resident-agent-2", "cwd": str(workspace), "label": "Second",
            }))
            self.assertTrue(refused["atCapacity"])
            self.assertEqual(refused["limit"], 1)
            self.assertEqual(len(refused["children"]), 1)
            self.assertEqual(refused["children"][0]["agentId"], "resident-agent")
            self.assertEqual(refused["children"][0]["status"], "awaiting_input")
            self.assertTrue(any(event.get("event") == "subagent.capacity" for event in events))

            stopped = run(supervisor["stopTask"](task_id))
            self.assertEqual(stopped["status"], "stopped")
            second_started = run(supervisor["startSubagentTask"]({
                "parentSessionId": parent["id"], "sessionId": second_child["id"],
                "agentId": "resident-agent-2", "cwd": str(workspace), "label": "Second",
            }))
            self.assertIn("taskId", second_started)
            run(supervisor["stopTask"](second_started["taskId"]))
        finally:
            os.environ.pop("AGENT_CORE_SUBAGENTS_MAX_LIVE", None)
            os.environ.pop("AGENT_CORE_SUBAGENTS_RESIDENT_POLL_MS", None)
            if supervisor:
                run(supervisor["close"]())
            self.restore_home(root, previous)
