from __future__ import annotations

import tempfile
from types import SimpleNamespace
from typing import Any

from tests.support.agent_core_case import (
    AgentCoreTestCase,
    AsyncChunks,
    bind_test_runtime,
    core,
    run,
    testing,
)


class _SdkProcessingError(Exception):
    __module__ = "openai"

    def __init__(self) -> None:
        super().__init__("An error occurred while processing your request. You can retry.")
        self.body = {"error": {"type": "server_error", "code": "internal_error"}}
        self.request_id = "req-after-tool"


def _config(cwd: str) -> dict[str, Any]:
    return {
        "cwd": cwd,
        "runtimeHome": cwd,
        "allowedRoots": [cwd],
        "agents": {},
        "provider": "openai",
        "providerKind": "openai",
        "providerProfileName": "codex",
        "providerProfile": {"kind": "openai", "name": "codex", "auth": {}},
        "providerProfiles": {"codex": {"kind": "openai", "name": "codex"}},
        "customHeaders": {},
        "openaiApiMode": "responses",
        "model": "gpt-5.6-sol",
        "effort": "high",
        "assistantName": "Test",
        "assistantDescription": "",
        "systemPrompt": "stable",
        "tools": ["Read"],
        "allowedTools": ["Read"],
        "disallowedTools": [],
        "permissionMode": "bypassPermissions",
        "sessionPersistence": False,
    }


class Package43AgentLoopSafetyTests(AgentCoreTestCase):
    def test_retry_after_prior_completed_tool_does_not_reexecute_tool_iteration(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-core-p43-tool-") as cwd:
            session = core.create_empty_session(
                cwd=cwd,
                provider="openai",
                model="gpt-5.6-sol",
                effort="high",
                systemPrompt="stable",
                permissionMode="bypassPermissions",
            )
            handler_calls = 0
            provider_iterations = 0
            create_calls: list[dict[str, Any]] = []
            events: list[tuple[str, dict[str, Any]]] = []

            async def read_handler(_input: dict[str, Any], _context: dict[str, Any]) -> dict[str, Any]:
                nonlocal handler_calls
                handler_calls += 1
                return {"content": "stable tool result", "isError": False}

            tool = core.ToolDefinition(
                "Read", "Read", {"type": "object"}, read_handler
            )

            async def create(params: dict[str, Any]) -> AsyncChunks:
                create_calls.append(params)
                if len(create_calls) == 1:
                    raise _SdkProcessingError()
                return AsyncChunks(
                    [
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "resp-final",
                                "output": [
                                    {
                                        "type": "message",
                                        "content": [
                                            {
                                                "type": "output_text",
                                                "text": "finished after recovery",
                                            }
                                        ],
                                    }
                                ],
                                "usage": {"input_tokens": 5, "output_tokens": 3},
                            },
                        }
                    ]
                )

            async def send(request: dict[str, Any]) -> dict[str, Any]:
                nonlocal provider_iterations
                provider_iterations += 1
                if provider_iterations == 1:
                    return {
                        "model": "gpt-5.6-sol",
                        "stopReason": "tool_calls",
                        "usage": {"input_tokens": 4, "output_tokens": 2},
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call-stable",
                                "name": "Read",
                                "input": {"path": "evidence"},
                            }
                        ],
                    }
                return await testing.send_openai_responses_request(
                    token="test",
                    mode="codex",
                    request=request,
                    runtime={
                        "emitEvent": lambda event, data: events.append((event, data))
                    },
                    maxAttempts=2,
                    retryDelay=lambda _attempt: 0,
                    clientFactory=lambda _options: SimpleNamespace(
                        responses=SimpleNamespace(create=create)
                    ),
                )

            result = run(
                core.runAgentPrompt(
                    session=session,
                    config=_config(cwd),
                    runtime=bind_test_runtime(
                        session,
                        {
                            "emitEvent": lambda event, data: events.append((event, data)),
                            "tools": [tool],
                        },
                    ),
                    prompt="run",
                    providerOverride={"send": send},
                )
            )

            self.assertEqual(result["text"], "finished after recovery")
            self.assertEqual(handler_calls, 1)
            self.assertEqual(provider_iterations, 2)
            self.assertEqual(len(create_calls), 2)
            self.assertEqual(create_calls[0]["input"], create_calls[1]["input"])
            function_outputs = [
                item for item in create_calls[0]["input"] if item["type"] == "function_call_output"
            ]
            self.assertEqual(
                function_outputs,
                [
                    {
                        "type": "function_call_output",
                        "call_id": "call-stable",
                        "output": "stable tool result",
                    }
                ],
            )
            self.assertEqual(
                [event for event, _data in events].count("tool.started"), 1
            )
            self.assertEqual(
                [event for event, _data in events].count("provider.retrying"), 1
            )
