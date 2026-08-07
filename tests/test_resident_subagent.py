from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core.tasks.communication import read_task_mailbox_messages, send_task_message


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
        config = run(core.resolveConfig(
            {"provider": "resident-openai", "settings": str(settings_path), "runtimeHome": str(home), "permissionMode": "bypassPermissions"},
            str(workspace),
        ))
        parent = core.createEmptySession(
            cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"],
            model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"],
            permissionMode="bypassPermissions",
        )
        child = core.createEmptySession(
            cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"],
            model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"],
            permissionMode="bypassPermissions",
        )
        child["messages"].append({"role": "user", "content": "report in"})
        child["metadata"]["parentSessionId"] = parent["id"]
        child["metadata"]["agentId"] = "resident-agent"
        child["metadata"]["agentCoreSettingsPath"] = str(settings_path)
        child_authority = run(core.captureAgentAuthority(config=config, session=child, tools=core.createBuiltinTools()))
        child["metadata"]["effectiveAgentAuthority"] = child_authority
        run(core.saveSession(parent))
        run(core.saveSession(child))
        run(core.saveChildAgent({
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
            supervisor = run(core.createTaskSupervisor({"cwd": str(workspace)}))
            started = run(supervisor["startSubagentTask"]({
                "parentSessionId": parent["id"], "sessionId": child["id"],
                "agentId": "resident-agent", "cwd": str(workspace), "label": "Resident",
            }))
            task_id = started["taskId"]

            meta = self._wait_for(
                lambda: (lambda m: m if m and m.get("status") == "awaiting_input" else None)(run(core.readTaskMeta(task_id))),
                label="resident idle after initial turn",
            )
            self.assertEqual(meta["waitingReason"], "resident_idle")
            child_record = run(core.loadChildAgent("resident-agent"))
            self.assertEqual(child_record["status"], "awaiting_input")
            self.assertTrue(child_record["stoppable"])
            self.assertIsNone(child_record.get("completedAt"))
            task_record = run(core.loadTaskRecord(task_id))
            self.assertEqual(task_record["status"], "awaiting_input")
            self.assertEqual(task_record["detailStatus"], "resident_idle")

            results = [m for m in run(read_task_mailbox_messages(task_id)) if m.get("kind") == "result" and m.get("direction") == "to_parent"]
            self.assertEqual(len(results), 1)
            self.assertIn("provider profile 'resident-openai'", results[0]["content"])

            baseline_messages = len(run(core.loadSession(child["id"]))["messages"])
            run(send_task_message(task_id, {"direction": "to_child", "content": "follow up please"}))
            self._wait_for(
                lambda: len([m for m in run(read_task_mailbox_messages(task_id)) if m.get("kind") == "result" and m.get("direction") == "to_parent"]) >= 2,
                label="second resident turn result",
            )
            meta = self._wait_for(
                lambda: (lambda m: m if m and m.get("status") == "awaiting_input" else None)(run(core.readTaskMeta(task_id))),
                label="idle again after continuation",
            )
            persisted_child = run(core.loadSession(child["id"]))
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
