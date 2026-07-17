from __future__ import annotations

import copy

from tests.support.agent_core_case import *

from agent_core.shell_surface.status import buildTodosReport
from agent_core.tool_result_guard import TOOL_RESULT_FINAL_BYTES, serializedContentBytes


class Package31TodoCardinalityScenarioTests(AgentCoreTestCase):
    def test_todo_cardinality_and_small_terminal_view_are_bounded(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                tool = next(tool for tool in core.createBuiltinTools() if tool.name == "TodoWrite")
                session = core.createEmptySession(
                    cwd=tmp,
                    provider="openai",
                    model="gpt-5.4",
                    systemPrompt="test",
                    permissionMode="bypassPermissions",
                )
                persist_count = 0

                async def persist():
                    nonlocal persist_count
                    persist_count += 1

                todos = [
                    {
                        "content": f"todo-{index}",
                        "status": "in_progress" if index % 2 == 0 else "pending",
                        "activeForm": f"doing-{index}",
                    }
                    for index in range(100)
                ]
                result = await tool.handler(
                    {"todos": todos},
                    {"session": session, "persistSession": persist},
                )
                payload = json.loads(result["content"])
                self.assertEqual(persist_count, 1)
                self.assertEqual(payload["originalItemCount"], 100)
                self.assertEqual(payload["returnedItemCount"], 20)
                self.assertEqual(payload["omittedItemCount"], 80)
                self.assertTrue(payload["itemsTruncated"])
                self.assertLessEqual(
                    serializedContentBytes(result["content"]),
                    TOOL_RESULT_FINAL_BYTES["small"],
                )

                with self.assertRaisesRegex(ValueError, "at most 100 items"):
                    await tool.handler(
                        {"todos": [*todos, {**todos[0], "content": "overflow"}]},
                        {"session": session, "persistSession": persist},
                    )
                self.assertEqual(persist_count, 1)

                session["todos"] = [
                    {**todos[index % len(todos)], "content": f"legacy-{index}"}
                    for index in range(60)
                ]
                before = copy.deepcopy(session["todos"])
                report = buildTodosReport(session)
                self.assertIn("40 omitted (20 of 60 shown)", report)
                self.assertLess(len(report.encode("utf-8")), 50_000)
                self.assertEqual(session["todos"], before)

        run(scenario())


if __name__ == "__main__":
    unittest.main()
