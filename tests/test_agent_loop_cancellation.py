from __future__ import annotations

from tests.support.agent_core_case import *


class AgentLoopCancellationTests(AgentCoreTestCase):
    def test_first_turn_cancellation_clears_the_already_persisted_prompt(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = {
                "cwd": str(workspace),
                "runtimeHome": str(home),
                "allowedRoots": [str(workspace)],
                "agents": {},
                "provider": "openai",
                "providerKind": "openai",
                "providerProfileName": "test",
                "providerProfile": {"kind": "openai", "name": "test", "maxContextLength": 100_000, "maxOutputTokens": 4_096},
                "providerProfiles": {},
                "openaiApiMode": "responses",
                "model": "gpt-5.4",
                "effort": "medium",
                "systemPrompt": "test",
                "tools": [],
                "allowedTools": [],
                "disallowedTools": [],
                "permissionMode": "bypassPermissions",
                "sessionPersistence": True,
            }
            session = core.createEmptySession(
                cwd=str(workspace), provider="openai", model="gpt-5.4",
                systemPrompt="test", permissionMode="bypassPermissions",
                sessionId="cancelled-first-turn",
            )
            signal = SimpleNamespace(aborted=False, reason=None)

            async def send(_request):
                signal.aborted = True
                error = RuntimeError("provider aborted")
                error.name = "AbortError"
                signal.reason = error
                raise error

            result = run(core.runAgentPrompt(
                session=session,
                config=config,
                runtime=bind_test_runtime(session, {"abortSignal": signal, "tools": []}),
                prompt="do not retain me",
                providerOverride={"send": send},
            ))
            restored = run(core.loadSession(session["id"]))
            self.assertEqual(result["stopReason"], "cancelled")
            self.assertEqual(session["messages"], [])
            self.assertEqual(restored["messages"], [])
        finally:
            self.restore_home(root, previous)

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
