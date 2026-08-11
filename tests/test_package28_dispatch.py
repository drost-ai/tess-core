from __future__ import annotations
from tests.support.agent_core_case import *

from agent_core.agent.tool_dispatch import (
    create_serial_queue,
    resolve_tool_concurrency,
    run_tool_dispatch_batch,
)


class Package28DispatchTests(AgentCoreTestCase):
    def test_every_builtin_has_a_reviewed_concurrency_contract(self):
        tools = {tool.name: tool.concurrency for tool in core.create_builtin_tools()}
        self.assertEqual(
            {name for name, concurrency in tools.items() if concurrency == "parallel"},
            {
                "Read",
                "Glob",
                "Grep",
                "TodoWrite",
                "WebFetch",
                "WebSearch",
                "SetStatus", "SendMessage",
                "Config",
                "TaskOutput",
                "TaskMonitorStart",
                "TaskMonitorStop",
                "ListMcpResources",
                "ReadMcpResource",
                "ReadToolArtifact",
            },
        )
        self.assertNotIn(None, tools.values())
        undeclared = core.ToolDefinition("custom", "custom", {"type": "object"}, _ok_handler)
        invalid = core.ToolDefinition("invalid", "invalid", {"type": "object"}, _ok_handler)
        invalid.concurrency = "unsafe"  # type: ignore[assignment]
        self.assertEqual(resolve_tool_concurrency(undeclared), "exclusive")
        self.assertEqual(resolve_tool_concurrency(invalid), "exclusive")

    def test_mixed_scheduler_overlaps_parallel_and_serializes_exclusive_in_emission_order(self):
        async def exercise():
            parallel_started = asyncio.Event()
            exclusive_started = asyncio.Event()
            exclusive_active = False
            execution = []

            async def dispatch(tool_use):
                nonlocal exclusive_active
                name = tool_use["name"]
                execution.append((name, "start"))
                if name == "parallel":
                    parallel_started.set()
                    await asyncio.wait_for(exclusive_started.wait(), 1)
                else:
                    self.assertFalse(exclusive_active)
                    exclusive_active = True
                    exclusive_started.set()
                    await asyncio.wait_for(parallel_started.wait(), 1)
                    exclusive_active = False
                execution.append((name, "end"))
                return {"result": {"type": "tool_result", "tool_use_id": tool_use["id"], "content": name}}

            tool_uses = [
                {"type": "tool_use", "id": "e1", "name": "exclusive-1", "input": {}},
                {"type": "tool_use", "id": "p", "name": "parallel", "input": {}},
                {"type": "tool_use", "id": "e2", "name": "exclusive-2", "input": {}},
            ]
            batch = await run_tool_dispatch_batch(
                tool_uses=tool_uses,
                sequential=False,
                concurrency_of=lambda item: "parallel" if item["name"] == "parallel" else "exclusive",
                run=dispatch,
            )
            self.assertEqual([item["tool_use_id"] for item in batch["results"]], ["e1", "p", "e2"])
            self.assertLess(execution.index(("exclusive-1", "end")), execution.index(("exclusive-2", "start")))
            self.assertLess(execution.index(("parallel", "start")), execution.index(("exclusive-1", "end")))

        run(exercise())

    def test_undeclared_same_file_mutations_have_sequential_semantics(self):
        async def exercise():
            contents = "initial"
            first_started = asyncio.Event()
            release_first = asyncio.Event()
            second_started = asyncio.Event()

            async def first(_input, _context):
                nonlocal contents
                observed = contents
                first_started.set()
                await release_first.wait()
                contents = observed + ":first"
                return {"content": contents}

            async def second(_input, _context):
                nonlocal contents
                second_started.set()
                observed = contents
                contents = observed + ":second"
                return {"content": contents}

            definitions = {
                "first": core.ToolDefinition("first", "first", {"type": "object"}, first),
                "second": core.ToolDefinition("second", "second", {"type": "object"}, second),
            }

            async def dispatch(tool_use):
                result = await definitions[tool_use["name"]].handler({}, {})
                return {"result": {"type": "tool_result", "tool_use_id": tool_use["id"], "content": result["content"]}}

            batch_task = asyncio.create_task(
                run_tool_dispatch_batch(
                    tool_uses=[{"id": "one", "name": "first"}, {"id": "two", "name": "second"}],
                    sequential=False,
                    concurrency_of=lambda item: resolve_tool_concurrency(definitions[item["name"]]),
                    run=dispatch,
                )
            )
            await first_started.wait()
            self.assertFalse(second_started.is_set())
            release_first.set()
            batch = await batch_task
            self.assertTrue(second_started.is_set())
            self.assertEqual(contents, "initial:first:second")
            self.assertEqual([item["content"] for item in batch["results"]], ["initial:first", "initial:first:second"])

        run(exercise())

    def test_scheduler_pause_skips_only_later_exclusive_calls(self):
        async def exercise():
            ran = []

            async def dispatch(tool_use):
                ran.append(tool_use["name"])
                outcome = {
                    "result": {
                        "type": "tool_result",
                        "tool_use_id": tool_use["id"],
                        "content": tool_use["name"],
                    }
                }
                if tool_use["name"] == "pause":
                    outcome["pauseMessage"] = "approve"
                return outcome

            tool_uses = [
                {"id": "pause", "name": "pause"},
                {"id": "parallel", "name": "parallel"},
                {"id": "skipped", "name": "exclusive"},
            ]
            batch = await run_tool_dispatch_batch(
                tool_uses=tool_uses,
                sequential=False,
                concurrency_of=lambda item: "parallel" if item["name"] == "parallel" else "exclusive",
                run=dispatch,
            )
            self.assertCountEqual(ran, ["pause", "parallel"])
            self.assertTrue(batch["pausedForApproval"])
            self.assertEqual(batch["pausedMessage"], "approve")
            self.assertIn("Skipped", batch["results"][2]["content"])

        run(exercise())

    def test_scheduler_cancellation_drains_children(self):
        async def exercise():
            started = 0
            cleaned = 0
            all_started = asyncio.Event()
            all_cleaned = asyncio.Event()

            async def dispatch(tool_use):
                nonlocal started, cleaned
                started += 1
                if started == 2:
                    all_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned += 1
                    if cleaned == 2:
                        all_cleaned.set()
                return {"result": {"tool_use_id": tool_use["id"]}}

            task = asyncio.create_task(
                run_tool_dispatch_batch(
                    tool_uses=[{"id": "a", "name": "a"}, {"id": "b", "name": "b"}],
                    sequential=False,
                    concurrency_of=lambda _item: "parallel",
                    run=dispatch,
                )
            )
            await asyncio.wait_for(all_started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(all_cleaned.wait(), 1)
            self.assertEqual(cleaned, 2)

        run(exercise())

    def test_serial_commit_queue_does_not_overlap_and_survives_failure(self):
        async def exercise():
            queue = create_serial_queue()
            first_entered = asyncio.Event()
            release_first = asyncio.Event()
            second_attempted = asyncio.Event()
            second_entered = asyncio.Event()

            async def first_commit():
                first_entered.set()
                await release_first.wait()
                raise RuntimeError("first failed")

            async def second_commit():
                second_entered.set()

            async def second_caller():
                second_attempted.set()
                await queue.run(second_commit)

            first = asyncio.create_task(queue.run(first_commit))
            await first_entered.wait()
            second = asyncio.create_task(second_caller())
            await second_attempted.wait()
            self.assertFalse(second_entered.is_set())
            release_first.set()
            with self.assertRaises(RuntimeError):
                await first
            await second
            self.assertTrue(second_entered.is_set())

        run(exercise())

    def test_mcp_annotations_survive_discovery_and_generated_definition_creation(self):
        async def exercise():
            class Client:
                async def close(self):
                    return None

            async def connect(state, _cwd, _on_event):
                state.update(
                    {
                        "client": Client(),
                        "connected": True,
                        "state": "connected",
                        "tools": [
                            {
                                "server": "fake",
                                "name": "read",
                                "runtimeName": "mcp__fake__read",
                                "description": "read",
                                "inputSchema": {"type": "object"},
                                "annotations": {
                                    "title": "Reader",
                                    "readOnlyHint": True,
                                    "destructiveHint": False,
                                    "openWorldHint": True,
                                },
                            },
                            {
                                "server": "fake",
                                "name": "write",
                                "runtimeName": "mcp__fake__write",
                                "inputSchema": {"type": "object"},
                                "annotations": {"readOnlyHint": False},
                            },
                            {
                                "server": "fake",
                                "name": "unknown",
                                "runtimeName": "mcp__fake__unknown",
                                "inputSchema": {"type": "object"},
                            },
                        ],
                    }
                )

            with mock.patch.object(mcp_module.runtime, "_connect_state", connect):
                runtime = await core.create_mcp_runtime(
                    cwd="/tmp",
                    servers=[{"name": "fake", "transport": "http", "commandOrUrl": "https://example.test/mcp"}],
                )
                definitions = {tool.name: tool for tool in runtime["createToolDefinitions"]()}
                self.assertEqual(definitions["mcp__fake__read"].concurrency, "parallel")
                self.assertEqual(definitions["mcp__fake__read"].mcp["annotations"]["destructiveHint"], False)
                self.assertEqual(definitions["mcp__fake__write"].concurrency, "exclusive")
                self.assertEqual(definitions["mcp__fake__write"].mcp["annotations"]["readOnlyHint"], False)
                self.assertEqual(definitions["mcp__fake__unknown"].concurrency, "exclusive")
                self.assertNotIn("annotations", definitions["mcp__fake__unknown"].mcp)
                await runtime["close"]()

            class DiscoveryClient:
                async def request(self, _method, _params=None):
                    return {
                        "tools": [
                            {
                                "name": "edge",
                                "annotations": {
                                    "readOnlyHint": True,
                                    "destructiveHint": False,
                                    "futureHint": "ignored",
                                },
                            }
                        ]
                    }

            state = {"config": {"name": "fake"}, "client": DiscoveryClient(), "tools": [], "resources": []}
            discovered = await mcp_module.state._refresh_tools(state)
            self.assertEqual(discovered[0]["annotations"], {"readOnlyHint": True, "destructiveHint": False})

        run(exercise())

    def test_new_mcp_events_have_bounded_typed_payloads_and_runtime_projection(self):
        state = core.create_initial_runtime_state()
        authorization = core.create_runtime_event({
            "event": "mcp.authorization_required",
            "data": {
                "server": "fake",
                "transport": "http",
                "authorization_url": "https://auth.example/authorize?" + "x" * 10_000,
                "continuation_id": "continuation",
                "arguments": {"must": "not project"},
            },
        })
        self.assertEqual(authorization["kind"], "mcp.authorization_required")
        self.assertLessEqual(len(authorization["payload"]["authorizationUrl"].encode("utf-8")), 4096)
        self.assertNotIn("arguments", authorization["payload"])
        state = core.reduce_runtime_state(state, authorization)
        self.assertEqual(state["mcp"]["servers"]["fake"]["state"], "authorization_required")
        self.assertEqual(
            state["mcp"]["servers"]["fake"]["authorizationUrl"],
            "https://auth.example/authorize",
        )

        applied = core.create_runtime_event({
            "event": "mcp.catalog_changed_applied",
            "data": {"servers": ["fake"], "generation": 3, "tools_changed": True, "resources_changed": False},
        })
        state = core.reduce_runtime_state(state, applied)
        self.assertEqual(state["mcp"]["catalogGeneration"], 3)


async def _ok_handler(_input, _context):
    return {"content": "ok", "isError": False}
