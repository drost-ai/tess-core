from __future__ import annotations

from tests.support.agent_core_case import *


class AgentLoopCancellationTests(AgentCoreTestCase):
    def test_run_agent_prompt_cancels_turn_and_restores_session(self):
        with tempfile.TemporaryDirectory(prefix="agent-core-cancel-") as cwd:
            config = {
                "cwd": cwd,
                "runtimeHome": cwd,
                "allowedRoots": [cwd],
                "agents": {},
                "provider": "anthropic",
                "providerKind": "anthropic",
                "providerProfileName": "default",
                "providerProfile": {
                    "kind": "anthropic",
                    "name": "default",
                    "auth": {},
                },
                "providerProfiles": {
                    "default": {"kind": "anthropic", "name": "default"}
                },
                "customHeaders": {},
                "openaiApiMode": "responses",
                "model": "claude-sonnet-4-6",
                "effort": "medium",
                "assistantName": "Test",
                "assistantDescription": "",
                "systemPrompt": "test",
                "tools": [],
                "allowedTools": [],
                "disallowedTools": [],
                "permissionMode": "bypassPermissions",
                "sessionPersistence": False,
            }
            session = core.createEmptySession(
                cwd=cwd,
                provider="anthropic",
                model="claude-sonnet-4-6",
                effort="medium",
                systemPrompt="test",
                permissionMode="bypassPermissions",
            )
            signal = SimpleNamespace(aborted=False, reason=None)
            events = []

            async def send(_request):
                signal.aborted = True
                error = RuntimeError("provider aborted")
                error.name = "AbortError"
                raise error

            result = run(
                core.runAgentPrompt(
                    session=session,
                    config=config,
                    runtime=bind_test_runtime(
                        session,
                        {
                            "interactive": False,
                            "askQuestion": lambda *_: "",
                            "emitEvent": lambda event, data: events.append(
                                (event, data)
                            ),
                            "tools": [],
                            "abortSignal": signal,
                        },
                    ),
                    prompt="cancel this turn",
                    providerOverride={"send": send},
                )
            )
            self.assertEqual(result["stopReason"], "cancelled")
            self.assertEqual(session["messages"], [])
            self.assertTrue(
                any(
                    event == "turn.cancelled"
                    and data["session_id"] == session["id"]
                    for event, data in events
                )
            )
