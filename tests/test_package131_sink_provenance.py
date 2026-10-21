from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from tests.support.agent_core_case import AgentCoreTestCase, AsyncChunks, core, run, testing


class _Sink:
    schemaVersion = 2
    mode = "replaceable"

    def __init__(
        self,
        *,
        append_ack: bool = True,
        reconcile_ack: bool = True,
        commit_ack: bool = True,
    ) -> None:
        self.append_ack = append_ack
        self.reconcile_ack = reconcile_ack
        self.commit_ack = commit_ack
        self.operations: list[tuple[str, dict[str, Any]]] = []

    async def begin(self, operation: dict[str, Any]) -> bool:
        self.operations.append(("begin", dict(operation)))
        return True

    async def append(self, operation: dict[str, Any]) -> dict[str, Any]:
        self.operations.append(("append", dict(operation)))
        if not self.append_ack:
            return {"acknowledged": False, "reason": "append_rejected"}
        return {"acknowledged": True, "published": True}

    async def supersede(self, operation: dict[str, Any]) -> bool:
        self.operations.append(("supersede", dict(operation)))
        return True

    async def reconcile(self, operation: dict[str, Any]) -> dict[str, Any]:
        self.operations.append(("reconcile", dict(operation)))
        if not self.reconcile_ack:
            return {"acknowledged": False, "reason": "reconcile_failed"}
        return {"acknowledged": True}

    async def commit(self, operation: dict[str, Any]) -> dict[str, Any]:
        self.operations.append(("commit", dict(operation)))
        if not self.commit_ack:
            return {"acknowledged": False, "reason": "commit_failed"}
        return {"acknowledged": True}

    async def interrupt(self, operation: dict[str, Any]) -> bool:
        self.operations.append(("interrupt", dict(operation)))
        return True

    async def recover(self, operation: dict[str, Any]) -> bool:
        self.operations.append(("recover", dict(operation)))
        return True

    def names(self) -> list[str]:
        return [name for name, _operation in self.operations]


def _request() -> dict[str, Any]:
    return {
        "model": "gpt-5.6-sol",
        "system": "stable",
        "messages": [{"role": "user", "content": "work"}],
        "tools": [],
    }


def _text_events(text: str, terminal_output: list[Any] | None = None) -> list[dict[str, Any]]:
    if terminal_output is None:
        terminal_output = [
            {
                "type": "message",
                "id": "msg-1",
                "content": [{"type": "output_text", "text": text}],
            }
        ]
    return [
        {"type": "response.output_item.added", "item": {"id": "msg-1", "type": "message"}},
        {"type": "response.output_text.delta", "item_id": "msg-1", "delta": text},
        {
            "type": "response.output_item.done",
            "item": {
                "id": "msg-1",
                "type": "message",
                "content": [{"type": "output_text", "text": text}],
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp-1",
                "status": "completed",
                "output": terminal_output,
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
        },
    ]


def _tool_events(raw: str) -> list[dict[str, Any]]:
    function_call = {
        "type": "function_call",
        "id": "fc-1",
        "call_id": "call-1",
        "name": "Read",
        "arguments": raw,
    }
    return [
        {
            "type": "response.output_item.added",
            "item": {"id": "fc-1", "type": "function_call", "call_id": "call-1", "name": "Read"},
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": "fc-1",
            "name": "Read",
            "arguments": raw,
        },
        {"type": "response.output_item.done", "item": function_call},
        {
            "type": "response.completed",
            "response": {
                "id": "resp-1",
                "status": "completed",
                "output": [function_call],
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
        },
    ]


def _empty_completed() -> list[dict[str, Any]]:
    return [
        {
            "type": "response.completed",
            "response": {
                "id": "resp-1",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 4, "output_tokens": 0},
            },
        }
    ]


def _run_stream(
    sink: _Sink,
    events: list[dict[str, Any]],
    *,
    max_attempts: int = 1,
    create_override: Any = None,
    events_collector: list[tuple[str, dict[str, Any]]] | None = None,
) -> Any:
    async def create(_params: dict[str, Any]) -> AsyncChunks:
        return AsyncChunks(events)

    runtime: dict[str, Any] = {"partialOutputSink": sink}
    if events_collector is not None:
        runtime["emitEvent"] = lambda event, data: events_collector.append((event, data))
    return run(
        testing.send_openai_responses_request(
            token="test",
            mode="codex",
            request=_request(),
            runtime=runtime,
            maxAttempts=max_attempts,
            retryDelay=lambda _attempt: 0,
            clientFactory=lambda _options: SimpleNamespace(
                responses=SimpleNamespace(create=create_override or create)
            ),
        )
    )


class Package131SinkProvenanceTests(AgentCoreTestCase):
    def test_tool_only_response_skips_textual_sink_ops(self) -> None:
        sink = _Sink()
        raw = json.dumps({"path": "README.md"})
        response = _run_stream(sink, _tool_events(raw))
        self.assertEqual(response["stopReason"], "tool_calls")
        tool_blocks = [b for b in response["content"] if b.get("type") == "tool_use"]
        self.assertEqual(len(tool_blocks), 1)
        self.assertEqual(tool_blocks[0]["input"], {"path": "README.md"})
        names = sink.names()
        self.assertNotIn("reconcile", names)
        self.assertNotIn("commit", names)

    def test_empty_completed_response_is_protocol_retryable(self) -> None:
        sink = _Sink()
        with self.assertRaises(core.ProviderRequestError) as ctx:
            _run_stream(sink, _empty_completed(), max_attempts=1)
        record = ctx.exception.record
        self.assertEqual(record["category"], "protocol")
        self.assertTrue(record["retryable"])
        self.assertFalse(record["hadAssistantOutput"])
        self.assertFalse(record["hadToolUse"])
        self.assertNotIn("commit", sink.names())
        self.assertNotIn("reconcile", sink.names())

    def test_empty_completed_response_recovers_on_retry(self) -> None:
        sink = _Sink()
        calls = 0

        async def create(_params: dict[str, Any]) -> AsyncChunks:
            nonlocal calls
            calls += 1
            if calls == 1:
                return AsyncChunks(_empty_completed())
            return AsyncChunks(_text_events("recovered"))

        response = _run_stream(sink, [], max_attempts=2, create_override=create)
        text = "".join(
            str(block.get("text", ""))
            for block in response["content"]
            if block.get("type") == "text"
        )
        self.assertEqual(text, "recovered")
        self.assertEqual(sink.names().count("commit"), 1)

    def test_sink_append_rejection_is_consumer_sink(self) -> None:
        sink = _Sink(append_ack=False)
        with self.assertRaises(core.ProviderRequestError) as ctx:
            _run_stream(sink, _text_events("hello"), max_attempts=1)
        record = ctx.exception.record
        self.assertEqual(record["category"], "consumer_sink")
        self.assertFalse(record["retryable"])
        self.assertEqual(record.get("component"), "partial_output_sink")
        self.assertEqual(record.get("operation"), "append")
        self.assertNotIn("malformed or incomplete", record["message"])

    def test_sink_reconcile_rejection_after_selection_degrades(self) -> None:
        sink = _Sink(reconcile_ack=False)
        emitted: list[tuple[str, dict[str, Any]]] = []
        response = _run_stream(sink, _text_events("kept"), events_collector=emitted)
        text = "".join(
            str(block.get("text", ""))
            for block in response["content"]
            if block.get("type") == "text"
        )
        self.assertEqual(text, "kept")
        metadata = response["provider_metadata"]
        receipt = metadata.get("partialOutputCommit") or {}
        self.assertTrue(receipt.get("degraded"))
        self.assertEqual(receipt.get("degradedOperation"), "reconcile")
        warnings = [data for event, data in emitted if event == "provider.consumer_sink_degraded"]
        self.assertTrue(warnings)
        self.assertNotIn("commit", sink.names())

    def test_sink_commit_rejection_after_selection_degrades(self) -> None:
        sink = _Sink(commit_ack=False)
        emitted: list[tuple[str, dict[str, Any]]] = []
        response = _run_stream(sink, _text_events("kept"), events_collector=emitted)
        text = "".join(
            str(block.get("text", ""))
            for block in response["content"]
            if block.get("type") == "text"
        )
        self.assertEqual(text, "kept")
        receipt = response["provider_metadata"].get("partialOutputCommit") or {}
        self.assertTrue(receipt.get("degraded"))
        self.assertEqual(receipt.get("degradedOperation"), "commit")
        warnings = [data for event, data in emitted if event == "provider.consumer_sink_degraded"]
        self.assertTrue(warnings)

    def test_consumer_sink_failure_is_not_provider_labeled(self) -> None:
        sink = _Sink(append_ack=False)
        with self.assertRaises(core.ProviderRequestError) as ctx:
            _run_stream(sink, _text_events("hello"), max_attempts=1)
        message = ctx.exception.record["message"]
        self.assertNotIn("OpenAI/Codex returned", message)
        self.assertNotIn("malformed", message)

    def test_commit_happens_once_across_conflict_recovery(self) -> None:
        sink = _Sink()
        calls = 0

        async def create(_params: dict[str, Any]) -> AsyncChunks:
            nonlocal calls
            calls += 1
            if calls == 1:
                return AsyncChunks(_text_events("hello", terminal_output=[]))
            return AsyncChunks(_text_events("recovered"))

        # Empty terminal with complete accumulated text selects the
        # accumulated projection; no regeneration is needed. This proves the
        # exact logical call commits exactly once with no empty reconcile.
        response = _run_stream(sink, [], max_attempts=2, create_override=create)
        text = "".join(
            str(block.get("text", ""))
            for block in response["content"]
            if block.get("type") == "text"
        )
        self.assertEqual(text, "hello")
        self.assertEqual(calls, 1)
        names = sink.names()
        self.assertEqual(names.count("commit"), 1)
        self.assertEqual(names.count("reconcile"), 1)

    def test_reasoning_only_tool_call_commits_and_does_not_block_the_next_call(self) -> None:
        from agent_core.providers.openai.recovery import commit_openai_delivery
        from agent_core.providers.partial_output import PartialOutputDelivery
        from agent_core.providers.partial_output_state import (
            create_partial_output_state_store,
            finalize_partial_output_state,
        )

        root, _home, _workspace, previous = self.temp_context()
        try:
            # A sinkless streaming host: the reasoning preview counts as published.
            session_id = "session-package131-reasoning-only"
            store = create_partial_output_state_store(session_id)
            runtime = {"partialOutputStateStore": store, "includePartialMessages": True}

            async def tool_call_turn() -> None:
                delivery = PartialOutputDelivery(runtime, "call-17", session_id=session_id)
                self.assertTrue((await delivery.begin(1))["acknowledged"])
                await delivery.append(1, delta="Inspecting schedules", text="Inspecting schedules",
                                      response_id="resp-17", sequence_number=4, content_kind="reasoning")
                receipt = await commit_openai_delivery(
                    delivery, generation=1, authoritative_text="", reasoning_text="Inspecting schedules",
                    response_id="resp-17", provider_label="OpenAI/Codex", phase="completed",
                    had_output=True, had_tool_use=True, logical_call_id="call-17", has_text=False,
                )
                self.assertTrue(await finalize_partial_output_state(store, receipt))
                self.assertEqual((await store.load())["terminalState"], "committed")
                after = PartialOutputDelivery(runtime, "call-18", session_id=session_id)
                self.assertTrue((await after.begin(1))["acknowledged"])

            run(tool_call_turn())
        finally:
            self.restore_home(root, previous)
