from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core.tasks.subagent_parts.permissions import inheritParentPermissionMode


class Package28PlanInheritanceTests(AgentCoreTestCase):
    def test_inherited_child_plan_gate_ignores_workspace_approval_lookalike(self):
        async def exercise():
            with tempfile.TemporaryDirectory(prefix="agent-core-p28-child-plan-") as cwd:
                lookalike = Path(cwd) / ".tess" / "project" / "mcp-approvals.json"
                lookalike.parent.mkdir(parents=True)
                lookalike.write_text(
                    json.dumps({"servers": {"fake": {"tools": {"mutate": "allow"}}}}),
                    encoding="utf-8",
                )

                child_mode = inheritParentPermissionMode("bypassPermissions", "plan")
                self.assertEqual(child_mode, "plan")
                config = await core.resolveConfig(
                    {
                        "provider": "openai",
                        "runtimeHome": str(Path(cwd) / "runtime"),
                        "permissionMode": child_mode,
                    },
                    cwd,
                )
                config.update(
                    {
                        "sessionPersistence": False,
                        "tools": ["mcp__fake__mutate"],
                        "allowedTools": ["mcp__fake__mutate"],
                        "disallowedTools": [],
                    }
                )
                session = core.createEmptySession(
                    cwd=cwd,
                    provider=config["provider"],
                    providerProfileName=config["providerProfileName"],
                    model=config["model"],
                    effort=config["effort"],
                    systemPrompt=config["systemPrompt"],
                    permissionMode=child_mode,
                )
                executed = []
                events = []

                async def handler(_input, _context):
                    executed.append(True)
                    return {"content": "mutated"}

                tool = core.ToolDefinition(
                    "mcp__fake__mutate",
                    "mutation",
                    {"type": "object"},
                    handler,
                    mcp={"server": "fake", "name": "mutate"},
                )
                sends = 0

                async def send(request):
                    nonlocal sends
                    sends += 1
                    if sends == 1:
                        return {
                            "model": config["model"],
                            "stopReason": "tool_use",
                            "usage": {},
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "child-mutation",
                                    "name": tool.name,
                                    "input": {},
                                }
                            ],
                        }
                    result = next(
                        block
                        for message in request["messages"]
                        if isinstance(message.get("content"), list)
                        for block in message["content"]
                        if block.get("type") == "tool_result"
                    )
                    self.assertTrue(result["is_error"])
                    self.assertIn("not_read_only", result["content"])
                    return {
                        "model": config["model"],
                        "stopReason": "end_turn",
                        "usage": {},
                        "content": [{"type": "text", "text": "blocked"}],
                    }

                await core.runAgentPrompt(
                    session=session,
                    config=config,
                    runtime=bind_test_runtime(session, {
                        "interactive": False,
                        "emitEvent": lambda event, data: events.append((event, data)),
                        "tools": [tool],
                        "repositoryIdentity": "trusted-runtime-identity",
                    }),
                    prompt="try mutation",
                    providerOverride={"send": send},
                )
                return executed, events

        executed, events = run(exercise())
        self.assertEqual(executed, [])
        self.assertIn(
            (
                "plan.mcp_tool_blocked",
                {"server": "fake", "tool": "mutate", "reason": "not_read_only"},
            ),
            events,
        )
