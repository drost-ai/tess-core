from __future__ import annotations

from tests.support.agent_core_case import *


class RuntimeBindingsContractTests(AgentCoreTestCase):
    def test_invalid_bindings_fail_before_any_execution_boundary(self):
        with tempfile.TemporaryDirectory(prefix="agent-core-bindings-invalid-") as cwd:
            session = core.create_empty_session(
                cwd=cwd,
                provider="openai",
                model="gpt-5.4",
                effort="medium",
                systemPrompt="test",
                permissionMode="bypassPermissions",
            )
            config = _config(cwd)
            boundary = {"provider": 0, "tool": 0, "event": 0, "refresh": 0, "daemon": 0}

            async def handler(_input, _context):
                boundary["tool"] += 1
                return {"content": "unexpected"}

            async def refresh():
                boundary["refresh"] += 1
                return None

            tool = core.ToolDefinition(
                "BoundaryProbe", "must never run", {"type": "object"}, handler
            )
            valid = core.create_agent_runtime_bindings(
                {
                    "tools": [tool],
                    "emitEvent": lambda *_: boundary.__setitem__(
                        "event", boundary["event"] + 1
                    ),
                    "daemon": {"state": "connected", "client": _gateway(boundary)},
                    "refreshMcpToolRegistry": refresh,
                    "sessionBinding": core.create_runtime_session_binding(session["id"], 1, 1),
                    "questionPolicy": {"interactive": False, "askQuestion": lambda *_: ""},
                }
            )
            cases = [
                ("missing", None, "runtime_bindings_missing"),
                ("partial", {}, "runtime_bindings_invalid"),
                ("old", {**valid, "schemaVersion": 0}, "runtime_bindings_upgrade_required"),
                ("future", {**valid, "schemaVersion": 2}, "runtime_bindings_unsupported"),
                (
                    "missing refresh",
                    {**valid, "refreshMcpToolRegistry": None},
                    "runtime_bindings_invalid",
                ),
                (
                    "partial question policy",
                    {**valid, "questionPolicy": {"interactive": False}},
                    "runtime_bindings_invalid",
                ),
                (
                    "invalid connected daemon",
                    {**valid, "daemon": {"state": "connected"}},
                    "runtime_bindings_invalid",
                ),
                (
                    "invalid unavailable daemon",
                    {
                        **valid,
                        "daemon": {
                            "state": "unavailable",
                            "reason": {"code": "", "message": ""},
                        },
                    },
                    "runtime_bindings_invalid",
                ),
                (
                    "wrong session",
                    {
                        **valid,
                        "sessionBinding": core.create_runtime_session_binding("other", 1, 1),
                    },
                    "runtime_bindings_invalid",
                ),
            ]

            async def send(_request):
                boundary["provider"] += 1
                return {"model": "gpt-5.4", "stopReason": "end_turn", "usage": {}, "content": []}

            for label, runtime, code in cases:
                with self.subTest(label=label):
                    with self.assertRaises(core.RuntimeBindingsCompatibilityError) as raised:
                        run(
                            core.runAgentPrompt(
                                session=session,
                                config=config,
                                runtime=runtime,
                                prompt="must not execute",
                                providerOverride={"send": send},
                            )
                        )
                    self.assertEqual(raised.exception.code, code)
                    self.assertLessEqual(len(str(raised.exception)), 256)
                    self.assertEqual(session["messages"], [])
            self.assertEqual(
                boundary,
                {"provider": 0, "tool": 0, "event": 0, "refresh": 0, "daemon": 0},
            )
            self.assertEqual(list(Path(cwd).iterdir()), [])

    def test_current_unavailable_binding_preserves_foreground_turn(self):
        with tempfile.TemporaryDirectory(prefix="agent-core-bindings-current-") as cwd:
            session = core.create_empty_session(
                cwd=cwd,
                provider="openai",
                model="gpt-5.4",
                effort="medium",
                systemPrompt="test",
                permissionMode="bypassPermissions",
            )
            config = _config(cwd)
            provider_calls = 0
            bindings = core.create_agent_runtime_bindings(
                {
                    "tools": [],
                    "emitEvent": lambda *_: None,
                    "daemon": {
                        "state": "unavailable",
                        "reason": {
                            "code": "platform.daemon_unavailable",
                            "message": "Daemon unavailable.",
                        },
                    },
                    "refreshMcpToolRegistry": lambda: None,
                    "sessionBinding": core.create_runtime_session_binding(session["id"], 4, 7),
                    "questionPolicy": {"interactive": False, "askQuestion": lambda *_: ""},
                }
            )

            async def send(_request):
                nonlocal provider_calls
                provider_calls += 1
                return {
                    "model": "gpt-5.4",
                    "stopReason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "content": [{"type": "text", "text": "foreground survived"}],
                }

            result = run(
                core.runAgentPrompt(
                    session=session,
                    config=config,
                    runtime={**bindings},
                    prompt="continue",
                    providerOverride={"send": send},
                )
            )
            self.assertEqual(result["text"], "foreground survived")
            self.assertEqual(provider_calls, 1)
            self.assertEqual(bindings["sessionBinding"]["generation"], 4)
            self.assertIsInstance(bindings["tools"], tuple)
            with self.assertRaises(TypeError):
                bindings["schemaVersion"] = 2


def _config(cwd):
    return {
        "cwd": cwd,
        "runtimeHome": cwd,
        "allowedRoots": [cwd],
        "agents": {},
        "provider": "openai",
        "providerKind": "openai",
        "providerProfileName": "default",
        "providerProfile": {"kind": "openai", "name": "default", "auth": {}},
        "providerProfiles": {"default": {"kind": "openai", "name": "default"}},
        "customHeaders": {},
        "openaiApiMode": "responses",
        "model": "gpt-5.4",
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


def _gateway(boundary):
    def call(*_args, **_kwargs):
        boundary["daemon"] += 1

    return {
        name: call
        for name in (
            "startBackgroundCommand",
            "startMainSessionTask",
            "startSubagentTask",
            "startHealthCheck",
            "stopTask",
            "retryTask",
            "read_task_output",
            "sendTaskMessage",
            "readTaskEvents",
            "readTaskQuestions",
            "answerTaskQuestion",
            "skipTaskQuestion",
        )
    }
