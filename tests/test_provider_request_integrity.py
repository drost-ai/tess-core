from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core.agent.request_integrity import normalizeProviderRequestMessages

def _tool_use(call_id: str, name: str = "Read") -> dict:
    return {"role": "assistant", "content": [{"type": "tool_use", "id": call_id, "name": name, "input": {}}]}


def _tool_result(call_id: str, content: str = "tool output") -> dict:
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": content, "is_error": False}]}


class ProviderRequestIntegrityTests(AgentCoreTestCase):
    def test_orphaned_tool_result_at_request_start_is_textified(self):
        normalized = normalizeProviderRequestMessages(
            "anthropic",
            [
                _tool_result("toolu_missing", "report body"),
                {"role": "user", "content": "continue"},
            ],
        )

        messages = normalized["messages"]
        self.assertTrue(normalized["repair"]["changed"])
        self.assertEqual(normalized["repair"]["orphanedToolResults"], 1)
        self.assertEqual(messages[0]["role"], "user")
        self.assertIsInstance(messages[0]["content"], str)
        self.assertIn("Historical tool context", messages[0]["content"])
        self.assertNotIsInstance(messages[0]["content"], list)

    def test_missing_tool_result_gets_synthetic_result_before_next_user(self):
        normalized = normalizeProviderRequestMessages(
            "anthropic",
            [
                _tool_use("call_1"),
                {"role": "user", "content": "next question"},
            ],
        )

        messages = normalized["messages"]
        self.assertTrue(normalized["repair"]["changed"])
        self.assertEqual(normalized["repair"]["syntheticToolResults"], 1)
        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(messages[1]["content"][0]["type"], "tool_result")
        self.assertEqual(messages[1]["content"][0]["tool_use_id"], "call_1")
        self.assertTrue(messages[1]["content"][0]["is_error"])
        self.assertEqual(messages[2]["content"], "next question")

    def test_valid_tool_rounds_are_left_unchanged(self):
        source = [
            {"role": "user", "content": "read"},
            _tool_use("call_1"),
            _tool_result("call_1", "ok"),
            {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        ]

        normalized = normalizeProviderRequestMessages("anthropic", source)

        self.assertFalse(normalized["repair"]["changed"])
        self.assertEqual(normalized["messages"], source)

    def test_request_repair_handles_tail_start_inside_tool_exchange(self):
        messages = [{"role": "user", "content": "start long research"}]
        for index in range(8):
            messages.append(_tool_use(f"call_{index}"))
            messages.append(_tool_result(f"call_{index}", "x" * 1000))

        fallback = max(0, len(messages) - 12)
        self.assertNotEqual(fallback, 0)
        sliced = messages[fallback + 1 :]

        normalized = normalizeProviderRequestMessages("anthropic", sliced)

        self.assertTrue(normalized["repair"]["changed"])
        self.assertEqual(normalized["messages"][0]["role"], "user")
        first_content = normalized["messages"][0]["content"]
        if isinstance(first_content, list):
            self.assertNotEqual(first_content[0].get("type"), "tool_result")
        else:
            self.assertIn("Historical tool context", first_content)
