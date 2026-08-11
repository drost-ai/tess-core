from __future__ import annotations

import json
import unittest

import agent_core as core
from agent_core.context.compaction.projection import build_messages_for_working_context
from agent_core.session.tool_boundaries import (
    adjustStartToToolBoundary,
    findIncompleteToolExchange,
    sliceStartCrossesToolExchange,
)


def _tu(tool_id: str, name: str = "Read") -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": {}}


def _tr(tool_id: str) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}


class UnifiedToolBoundaryTests(unittest.TestCase):
    def test_split_result_batch_is_a_complete_exchange_and_survives_repair(self):
        messages = [
            {"role": "assistant", "content": [_tu("a"), _tu("b")]},
            {"role": "user", "content": [_tr("a")]},
            {"role": "user", "content": [_tr("b")]},
        ]
        self.assertIsNone(findIncompleteToolExchange(messages))
        session = {"messages": list(messages)}
        repair = core.repairSessionTranscript(session)
        self.assertFalse(repair["repaired"])
        self.assertEqual(len(session["messages"]), 3)

    def test_interposed_user_text_does_not_truncate_the_exchange(self):
        messages = [
            {"role": "assistant", "content": [_tu("a")]},
            {"role": "user", "content": "a note in between"},
            {"role": "user", "content": [_tr("a")]},
        ]
        self.assertIsNone(findIncompleteToolExchange(messages))
        session = {"messages": list(messages)}
        self.assertFalse(core.repairSessionTranscript(session)["repaired"])

    def test_missing_result_truncates_at_the_first_incomplete_exchange(self):
        messages = [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": [_tu("a")]},
            {"role": "user", "content": [_tr("a")]},
            {"role": "assistant", "content": [_tu("b")]},
        ]
        self.assertEqual(
            findIncompleteToolExchange(messages),
            {"index": 3, "missingToolUseIds": ["b"]},
        )
        session = {"messages": list(messages)}
        repair = core.repairSessionTranscript(session)
        self.assertTrue(repair["repaired"])
        self.assertEqual(len(session["messages"]), 3)

    def test_results_after_a_later_assistant_message_do_not_complete_the_exchange(self):
        messages = [
            {"role": "assistant", "content": [_tu("a")]},
            {"role": "assistant", "content": "interrupting"},
            {"role": "user", "content": [_tr("a")]},
        ]
        self.assertEqual(findIncompleteToolExchange(messages)["index"], 0)
        self.assertEqual(core.collect_substantive_tool_exchange_starts(messages, -1), [])

    def test_slice_start_adjustment_walks_past_interposed_text(self):
        messages = [
            {"role": "assistant", "content": [_tu("a")]},
            {"role": "user", "content": "note"},
            {"role": "user", "content": [_tr("a")]},
            {"role": "user", "content": "fresh request"},
        ]
        self.assertTrue(sliceStartCrossesToolExchange(messages, 1))
        self.assertTrue(sliceStartCrossesToolExchange(messages, 2))
        self.assertFalse(sliceStartCrossesToolExchange(messages, 3))
        self.assertEqual(adjustStartToToolBoundary(messages, 2), 0)
        self.assertEqual(adjustStartToToolBoundary(messages, 3), 3)

    def test_working_context_projection_never_ships_an_orphan_tool_result(self):
        session = {
            "messages": [
                {"role": "user", "content": "start"},
                {"role": "assistant", "content": [_tu("a")]},
                {"role": "user", "content": [_tr("a")]},
                {"role": "assistant", "content": "done"},
            ],
            "metadata": {"compactionState": {"compactedThroughMessageIndex": 2}},
        }
        working = build_messages_for_working_context(session)
        rendered = json.dumps(working)
        self.assertLess(rendered.find('"tool_use"'), rendered.find('"tool_result"'))
        self.assertEqual(working[0]["role"], "user")
        self.assertEqual(working[1]["content"][0]["type"], "tool_use")

    def test_id_less_tool_use_pairs_only_with_an_id_less_result(self):
        complete = [
            {"role": "assistant", "content": [{"type": "tool_use", "name": "X", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
        ]
        self.assertIsNone(findIncompleteToolExchange(complete))
        dangling = [complete[0]]
        self.assertEqual(
            findIncompleteToolExchange(dangling),
            {"index": 0, "missingToolUseIds": [""]},
        )


if __name__ == "__main__":
    unittest.main()
