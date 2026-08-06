from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from tests.support.agent_core_case import AgentCoreTestCase, AsyncChunks, core, run, testing
from agent_core.providers.partial_output import PartialOutputDelivery


class _Sink:
    schemaVersion = 2

    def __init__(
        self,
        mode: str = "replaceable",
        *,
        supersede_ack: bool = True,
        interrupt_ack: bool = True,
        supersede_delay: float = 0,
    ) -> None:
        self.mode = mode
        self.supersede_ack = supersede_ack
        self.interrupt_ack = interrupt_ack
        self.supersede_delay = supersede_delay
        self.operations: list[tuple[str, dict[str, Any]]] = []
        self.text = ""
        self.committed = ""

    async def begin(self, operation: dict[str, Any]) -> bool:
        self.operations.append(("begin", dict(operation)))
        return True

    async def append(self, operation: dict[str, Any]) -> dict[str, bool]:
        self.operations.append(("append", dict(operation)))
        self.text = str(operation["text"])
        return {"acknowledged": True, "published": self.mode != "buffered"}

    async def supersede(self, operation: dict[str, Any]) -> bool:
        self.operations.append(("supersede", dict(operation)))
        if self.supersede_delay:
            await asyncio.sleep(self.supersede_delay)
        if self.supersede_ack:
            self.text = ""
        return self.supersede_ack

    async def reconcile(self, operation: dict[str, Any]) -> bool:
        self.operations.append(("reconcile", dict(operation)))
        self.text = str(operation["text"])
        return True

    async def commit(self, operation: dict[str, Any]) -> bool:
        self.operations.append(("commit", dict(operation)))
        self.committed = str(operation["text"])
        return True

    async def interrupt(self, operation: dict[str, Any]) -> bool:
        self.operations.append(("interrupt", dict(operation)))
        if self.interrupt_ack:
            self.text = f"{operation['text']}\n\n[Response interrupted before completion.]"
        return self.interrupt_ack

    async def recover(self, operation: dict[str, Any]) -> bool:
        self.operations.append(("recover", dict(operation)))
        return True


def _request(messages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "model": "gpt-5.6-sol",
        "system": "stable",
        "messages": messages or [{"role": "user", "content": "work"}],
        "tools": [{"name": "Read", "description": "Read", "input_schema": {"type": "object"}}],
    }


def _completed(text: str, *, tool: dict[str, Any] | None = None) -> AsyncChunks:
    output: list[dict[str, Any]] = [
        {"type": "message", "content": [{"type": "output_text", "text": text}]}
    ]
    if tool is not None:
        output.append(tool)
    return AsyncChunks(
        [
            {
                "type": "response.completed",
                "response": {
                    "id": "resp-authoritative",
                    "output": output,
                    "usage": {"input_tokens": 4, "output_tokens": 2},
                },
            }
        ]
    )


def _failed_after_text(text: str = "obsolete fragment") -> AsyncChunks:
    return AsyncChunks(
        [
            {
                "type": "response.created",
                "response": {"id": "resp-incomplete"},
                "sequence_number": 0,
            },
            {
                "type": "response.output_text.delta",
                "delta": text,
                "sequence_number": 1,
            },
            {
                "type": "response.failed",
                "response": {"error": {"type": "server_error", "message": "upstream failed"}},
                "sequence_number": 2,
            },
        ]
    )


class Package43CodexSupersessionTests(AgentCoreTestCase):
    def test_partial_text_is_replaced_only_after_ack_and_full_replay_is_stable(self) -> None:
        sink = _Sink()
        calls: list[dict[str, Any]] = []
        events: list[tuple[str, dict[str, Any]]] = []

        async def create(params: dict[str, Any]) -> AsyncChunks:
            calls.append(params)
            if len(calls) == 1:
                return _failed_after_text()
            supersede_index = next(
                index
                for index, (name, _operation) in enumerate(sink.operations)
                if name == "supersede"
            )
            self.assertTrue(sink.supersede_ack)
            self.assertGreater(len(sink.operations), supersede_index)
            return _completed("authoritative answer")

        response = run(
            testing.sendOpenAiResponsesRequest(
                token="test",
                mode="codex",
                request=_request(),
                requestOptions={
                    "background": True,
                    "store": True,
                    "previous_response_id": "resp-previous",
                    "starting_after": 9,
                },
                runtime={
                    "includePartialMessages": True,
                    "partialOutputSink": sink,
                    "emitEvent": lambda event, data: events.append((event, data)),
                },
                maxAttempts=2,
                retryDelay=lambda _attempt: 0,
                clientFactory=lambda _options: SimpleNamespace(
                    responses=SimpleNamespace(create=create)
                ),
            )
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["input"], calls[1]["input"])
        self.assertEqual(calls[0]["instructions"], calls[1]["instructions"])
        for params in calls:
            self.assertEqual(params["store"], False)
            self.assertEqual(params["stream"], True)
            self.assertNotIn("background", params)
            self.assertNotIn("previous_response_id", params)
            self.assertNotIn("starting_after", params)
            self.assertNotIn("starting_after", params.get("extra_body", {}))
            self.assertNotIn("store", params.get("extra_body", {}))
        self.assertEqual(response["content"], [{"type": "text", "text": "authoritative answer"}])
        self.assertEqual(sink.committed, "authoritative answer")
        self.assertNotIn("obsolete fragment", sink.text)
        self.assertEqual(
            [name for name, _operation in sink.operations],
            ["begin", "append", "supersede", "begin", "reconcile", "commit"],
        )
        superseded = next(
            data for event, data in events if event == "provider.attempt_superseded"
        )
        recovered = next(data for event, data in events if event == "provider.recovered")
        self.assertEqual(superseded["generation"], 1)
        self.assertEqual(superseded["next_generation"], 2)
        self.assertEqual(recovered["generation"], 2)

    def test_replacement_rejection_or_timeout_never_starts_generation_two(self) -> None:
        for label, sink, timeout_ms in (
            ("rejected", _Sink(supersede_ack=False), 100),
            ("timed-out", _Sink(supersede_delay=0.05), 1),
        ):
            with self.subTest(sink=label, timeout_ms=timeout_ms):
                attempts = 0

                async def create(_params: dict[str, Any]) -> AsyncChunks:
                    nonlocal attempts
                    attempts += 1
                    return _failed_after_text()

                with self.assertRaises(core.ProviderRequestError) as raised:
                    run(
                        testing.sendOpenAiResponsesRequest(
                            token="test",
                            mode="codex",
                            request=_request(),
                            runtime={
                                "includePartialMessages": True,
                                "partialOutputSink": sink,
                                "partialOutputAckTimeoutMs": timeout_ms,
                            },
                            maxAttempts=2,
                            retryDelay=lambda _attempt: 0,
                            clientFactory=lambda _options: SimpleNamespace(
                                responses=SimpleNamespace(create=create)
                            ),
                        )
                    )

                self.assertEqual(attempts, 1)
                self.assertEqual(raised.exception.record["category"], "server")
                self.assertEqual(
                    raised.exception.record["recoveryFailure"],
                    "supersede_timeout" if timeout_ms == 1 else "supersede_rejected",
                )

    def test_append_only_publication_is_terminal_and_interrupted(self) -> None:
        sink = _Sink(mode="append_only")
        attempts = 0

        async def create(_params: dict[str, Any]) -> AsyncChunks:
            nonlocal attempts
            attempts += 1
            return _failed_after_text("published fragment")

        with self.assertRaises(core.ProviderRequestError) as raised:
            run(
                testing.sendOpenAiResponsesRequest(
                    token="test",
                    mode="codex",
                    request=_request(),
                    runtime={
                        "includePartialMessages": True,
                        "partialOutputSink": sink,
                    },
                    maxAttempts=2,
                    retryDelay=lambda _attempt: 0,
                    clientFactory=lambda _options: SimpleNamespace(
                        responses=SimpleNamespace(create=create)
                    ),
                )
            )

        self.assertEqual(attempts, 1)
        self.assertFalse(any(name == "supersede" for name, _ in sink.operations))
        self.assertTrue(any(name == "interrupt" for name, _ in sink.operations))
        receipt = raised.exception.record["interruptedPreview"]
        self.assertEqual(receipt["type"], "assistant_interrupted_preview")
        self.assertEqual(receipt["deliveryMode"], "append_only")
        self.assertTrue(receipt["deliveryAcknowledged"])
        self.assertNotIn("published fragment", str(receipt))

    def test_provisional_function_calls_are_discarded_without_dispatch(self) -> None:
        for complete_item in (False, True):
            with self.subTest(complete_item=complete_item):
                attempts = 0
                streams: list[AsyncChunks] = []
                first_events: list[dict[str, Any]] = [
                    {
                        "type": "response.output_item.added",
                        "item": {
                            "type": "function_call",
                            "id": "item-provisional",
                            "call_id": "call-provisional",
                            "name": "Read",
                        },
                    },
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": "item-provisional",
                        "name": "Read",
                        "arguments": '{"path":"unsafe"}',
                    },
                ]
                if complete_item:
                    first_events.append(
                        {
                            "type": "response.output_item.done",
                            "item": {
                                "type": "function_call",
                                "id": "item-provisional",
                                "call_id": "call-provisional",
                                "name": "Read",
                                "arguments": '{"path":"unsafe"}',
                            },
                        }
                    )
                first_events.append(
                    {
                        "type": "response.failed",
                        "response": {"error": {"type": "server_error", "message": "failed"}},
                    }
                )
                streams.append(AsyncChunks(first_events))
                streams.append(_completed("safe replacement"))

                async def create(_params: dict[str, Any]) -> AsyncChunks:
                    nonlocal attempts
                    attempts += 1
                    return streams.pop(0)

                response = run(
                    testing.sendOpenAiResponsesRequest(
                        token="test",
                        mode="codex",
                        request=_request(),
                        runtime={},
                        maxAttempts=2,
                        retryDelay=lambda _attempt: 0,
                        clientFactory=lambda _options: SimpleNamespace(
                            responses=SimpleNamespace(create=create)
                        ),
                    )
                )

                self.assertEqual(attempts, 2)
                self.assertEqual(
                    response["content"], [{"type": "text", "text": "safe replacement"}]
                )
                self.assertFalse(
                    any(
                        block.get("id") == "call-provisional"
                        for block in response["content"]
                    )
                )

    def test_committed_effect_fence_blocks_regeneration(self) -> None:
        attempts = 0

        async def create(_params: dict[str, Any]) -> AsyncChunks:
            nonlocal attempts
            attempts += 1
            return _failed_after_text()

        with self.assertRaises(core.ProviderRequestError):
            run(
                testing.sendOpenAiResponsesRequest(
                    token="test",
                    mode="codex",
                    request=_request(),
                    runtime={
                        "partialOutputSink": _Sink(),
                        "toolEffectCommitted": True,
                    },
                    maxAttempts=2,
                    retryDelay=lambda _attempt: 0,
                    clientFactory=lambda _options: SimpleNamespace(
                        responses=SimpleNamespace(create=create)
                    ),
                )
            )
        self.assertEqual(attempts, 1)

    def test_final_generation_visible_text_is_interrupted_without_extra_generation(self) -> None:
        sink = _Sink()
        attempts = 0

        async def create(_params: dict[str, Any]) -> AsyncChunks:
            nonlocal attempts
            attempts += 1
            return _failed_after_text(f"fragment {attempts}")

        with self.assertRaises(core.ProviderRequestError) as raised:
            run(
                testing.sendOpenAiResponsesRequest(
                    token="test",
                    mode="codex",
                    request=_request(),
                    runtime={"partialOutputSink": sink},
                    maxAttempts=2,
                    retryDelay=lambda _attempt: 0,
                    clientFactory=lambda _options: SimpleNamespace(
                        responses=SimpleNamespace(create=create)
                    ),
                )
            )

        self.assertEqual(attempts, 2)
        self.assertIn("fragment 2", sink.text)
        self.assertIn("interrupted", sink.text.lower())
        receipt = raised.exception.record["interruptedPreview"]
        self.assertEqual(receipt["generation"], 2)
        self.assertEqual(receipt["textLength"], len("fragment 2"))
        self.assertTrue(receipt["deliveryAcknowledged"])

    def test_buffered_partial_output_can_retry_without_publication(self) -> None:
        sink = _Sink(mode="buffered")
        attempts = 0

        async def create(_params: dict[str, Any]) -> AsyncChunks:
            nonlocal attempts
            attempts += 1
            return _failed_after_text() if attempts == 1 else _completed("buffered success")

        response = run(
            testing.sendOpenAiResponsesRequest(
                token="test",
                mode="codex",
                request=_request(),
                runtime={"partialOutputSink": sink},
                maxAttempts=2,
                retryDelay=lambda _attempt: 0,
                clientFactory=lambda _options: SimpleNamespace(
                    responses=SimpleNamespace(create=create)
                ),
            )
        )

        self.assertEqual(attempts, 2)
        self.assertEqual(response["content"], [{"type": "text", "text": "buffered success"}])
        self.assertEqual(sink.committed, "buffered success")

    def test_sink_operations_are_generation_fenced_and_idempotent(self) -> None:
        sink = _Sink()

        async def exercise() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
            delivery = PartialOutputDelivery(
                {"partialOutputSink": sink}, "logical-stale-proof"
            )
            self.assertTrue((await delivery.begin(1))["acknowledged"])
            first = await delivery.append(
                1,
                delta="one",
                text="one",
                response_id="resp-one",
                sequence_number=1,
            )
            duplicate = await delivery.append(
                1,
                delta="one",
                text="one",
                response_id="resp-one",
                sequence_number=1,
            )
            self.assertTrue(
                (await delivery.supersede(1, 2, "replace"))["acknowledged"]
            )
            self.assertTrue((await delivery.begin(2))["acknowledged"])
            stale = await delivery.append(
                1,
                delta="late",
                text="one late",
                response_id="resp-one",
                sequence_number=2,
            )
            self.assertTrue((await delivery.commit(2, "two", "resp-two"))["acknowledged"])
            repeated_commit = await delivery.commit(2, "two", "resp-two")
            return first, duplicate, stale | {"commit": repeated_commit}

        first, duplicate, stale = run(exercise())
        self.assertEqual(first, duplicate)
        self.assertFalse(stale["acknowledged"])
        self.assertEqual(stale["reason"], "stale_generation")
        self.assertTrue(stale["commit"]["acknowledged"])
        self.assertEqual(
            [name for name, _operation in sink.operations].count("append"), 1
        )
        self.assertEqual(
            [name for name, _operation in sink.operations].count("commit"), 1
        )
