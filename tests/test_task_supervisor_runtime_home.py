from __future__ import annotations

from tests.support.agent_core_case import *


class TaskSupervisorRuntimeHomeTests(AgentCoreTestCase):
    def test_background_subagent_worker_uses_explicit_runtime_home(self):
        root, home, workspace, previous = self.temp_context()
        wrong_home = Path(root.name) / "wrong-agent-core-home"
        supervisor = None
        try:
            settings_path = home / "settings.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "provider": "openai-codex",
                        "providerProfiles": {
                            "openai-codex": {
                                "kind": "openai",
                                "defaultModel": "gpt-5.5",
                                "auth": {
                                    "kind": "openai_api_key",
                                    "envVar": "AGENT_CORE_TEST_MISSING_OPENAI_KEY",
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            os.environ.pop("AGENT_CORE_TEST_MISSING_OPENAI_KEY", None)
            config = run(
                core.resolveConfig(
                    {
                        "provider": "openai-codex",
                        "settings": str(settings_path),
                        "runtimeHome": str(home),
                        "permissionMode": "bypassPermissions",
                    },
                    str(workspace),
                )
            )
            parent = core.createEmptySession(
                cwd=str(workspace),
                provider=config["provider"],
                providerProfileName=config["providerProfileName"],
                model=config["model"],
                effort=config["effort"],
                systemPrompt=config["systemPrompt"],
                permissionMode="bypassPermissions",
            )
            child = core.createEmptySession(
                cwd=str(workspace),
                provider=config["provider"],
                providerProfileName=config["providerProfileName"],
                model=config["model"],
                effort=config["effort"],
                systemPrompt=config["systemPrompt"],
                permissionMode="bypassPermissions",
            )
            child["messages"].append({"role": "user", "content": "finish without tools"})
            child["metadata"]["parentSessionId"] = parent["id"]
            child["metadata"]["agentId"] = "agent-runtime-home"
            child["metadata"]["agentCoreSettingsPath"] = str(settings_path)
            run(core.saveSession(parent))
            run(core.saveSession(child))
            os.environ["AGENT_CORE_HOME"] = str(wrong_home)

            async def _start_with_scoped_home():
                async with core.scopedRuntimeHome(str(home)):
                    supervisor_local = await core.createTaskSupervisor({"cwd": str(workspace)})
                    started_local = await supervisor_local["startSubagentTask"](
                        {
                            "parentSessionId": parent["id"],
                            "sessionId": child["id"],
                            "agentId": "agent-runtime-home",
                            "cwd": str(workspace),
                            "label": "Worker",
                            "runtimeHome": str(home),
                        }
                    )
                    return supervisor_local, started_local

            supervisor, started = run(_start_with_scoped_home())
            task_id = started["taskId"]
            for _ in range(100):

                async def _read_meta():
                    async with core.scopedRuntimeHome(str(home)):
                        return await core.readTaskMeta(task_id)

                meta = run(_read_meta())
                if meta and meta.get("status") == "completed":
                    break
                time.sleep(0.05)

            async def _read_final():
                async with core.scopedRuntimeHome(str(home)):
                    return await core.readTaskMeta(task_id), await supervisor["readTaskOutput"](
                        task_id,
                        12000,
                    )

            meta, output = run(_read_final())
            self.assertEqual(meta["status"], "completed")
            self.assertIn('"ok": true', output["output"])
            self.assertIn("provider profile 'openai-codex'", output["output"])
            self.assertFalse((wrong_home / "sessions" / f"{child['id']}.json").exists())
        finally:
            if supervisor:
                run(supervisor["close"]())
            self.restore_home(root, previous)
