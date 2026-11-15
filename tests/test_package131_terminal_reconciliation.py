from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from tests.support.tess_core_case import TessCoreTestCase, AsyncChunks, core, run, testing


class _Sink:
    schemaVersion = 2
    mode = "replaceable"

    def __init__(self) -> None:
        self.operations: list[tuple[str, dict[str, Any]]] = []

    async def begin(self, operation: dict[str, Any]) -> bool:
        self.operations.append(("begin", dict(operation)))
        return True

    async def append(self, operation: dict[str, Any]) -> dict[str, Any]:
        self.operations.append(("append", dict(operation)))
        return {"acknowledged": True, "published": True}

    async def supersede(self, operation: dict[str, Any]) -> bool:
        self.operations.append(("supersede", dict(operation)))
        return True

    async def reconcile(self, operation: dict[str, Any]) -> bool:
        self.operations.append(("reconcile", dict(operation)))
        return True

    async def commit(self, operation: dict[str, Any]) -> bool:
        self.operations.append(("commit", dict(operation)))
        return True

    async def interrupt(self, operation: dict[str, Any]) -> bool:
        self.operations.append(("interrupt", dict(operation)))
        return True

    async def recover(self, operation: dict[str, Any]) -> bool:
        self.operations.append(("recover", dict(operation)))
        return True

    def texts(self, name: str) -> list[str]:
        return [
            str(operation.get("text"))
            for op_name, operation in self.operations
            if op_name == name
        ]


def _request() -> dict[str, Any]:
    return {
        "model": "gpt-5.6-sol",
        "system": "stable",
        "messages": [{"role": "user", "content": "work"}],
        "tools": [],
    }


def _msg_added(item_id: str = "msg-1") -> dict[str, Any]:
    return {
        "type": "response.output_item.added",
        "item": {"id": item_id, "type": "message"},
    }


def _text_delta(delta: str, item_id: str = "msg-1") -> dict[str, Any]:
    return {"type": "response.output_text.delta", "item_id": item_id, "delta": delta}


def _msg_done(text: str, item_id: str = "msg-1") -> dict[str, Any]:
    return {
        "type": "response.output_item.done",
        "item": {
            "id": item_id,
            "type": "message",
            "content": [{"type": "output_text", "text": text}],
        },
    }


def _fc_added(item_id: str = "fc-1", call_id: str = "call-1", name: str = "Read") -> dict[str, Any]:
    return {
        "type": "response.output_item.added",
        "item": {"id": item_id, "type": "function_call", "call_id": call_id, "name": name},
    }


def _fc_args_done(raw: str, item_id: str = "fc-1", name: str = "Read") -> dict[str, Any]:
    return {
        "type": "response.function_call_arguments.done",
        "item_id": item_id,
        "name": name,
        "arguments": raw,
    }


def _fc_done(raw: str, item_id: str = "fc-1", call_id: str = "call-1", name: str = "Read") -> dict[str, Any]:
    return {
        "type": "response.output_item.done",
        "item": {
            "id": item_id,
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": raw,
        },
    }


def _completed(output: list[Any], response_id: str = "resp-1") -> dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "status": "completed",
            "output": output,
            "usage": {"input_tokens": 4, "output_tokens": 2},
        },
    }


def _terminal_message(text: str, item_id: str = "msg-1") -> dict[str, Any]:
    return {
        "type": "message",
        "id": item_id,
        "content": [{"type": "output_text", "text": text}],
    }


def _terminal_function_call(
    raw: str,
    item_id: str = "fc-1",
    call_id: str = "call-1",
    name: str = "Read",
) -> dict[str, Any]:
    return {
        "type": "function_call",
        "id": item_id,
        "call_id": call_id,
        "name": name,
        "arguments": raw,
    }


def _run_stream(
    sink: _Sink,
    events: list[dict[str, Any]],
    *,
    max_attempts: int = 1,
    create_override: Any = None,
    runtime_extra: dict[str, Any] | None = None,
) -> Any:
    async def create(_params: dict[str, Any]) -> AsyncChunks:
        return AsyncChunks(events)

    runtime: dict[str, Any] = {"partialOutputSink": sink}
    if runtime_extra:
        runtime.update(runtime_extra)
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


def _content_text(response: Any) -> str:
    return "".join(
        str(block.get("text", ""))
        for block in response["content"]
        if block.get("type") == "text"
    )


class Package131TerminalReconciliationTests(TessCoreTestCase):
    def test_empty_terminal_preserves_accumulated_text(self) -> None:
        sink = _Sink()
        accumulated = "x" * 606
        events = [
            _msg_added(),
            _text_delta(accumulated[:300]),
            _text_delta(accumulated[300:]),
            _msg_done(accumulated),
            _completed([]),
        ]
        response = _run_stream(sink, events)
        self.assertEqual(_content_text(response), accumulated)
        self.assertEqual(sink.texts("reconcile"), [accumulated])
        self.assertEqual(sink.texts("commit"), [accumulated])
        reconciliation = response["provider_metadata"]["reconciliation"]
        self.assertEqual(reconciliation["selected_source"], "incremental")
        self.assertEqual(reconciliation["terminal_block_count"], 0)
        self.assertEqual(reconciliation["incremental_block_count"], 1)

    def test_unrecognized_terminal_items_select_accumulated(self) -> None:
        sink = _Sink()
        events = [
            _msg_added(),
            _text_delta("kept"),
            _msg_done("kept"),
            _completed([{"type": "web_search_call", "id": "ws-1", "status": "completed"}]),
        ]
        response = _run_stream(sink, events)
        self.assertEqual(_content_text(response), "kept")
        reconciliation = response["provider_metadata"]["reconciliation"]
        self.assertEqual(reconciliation["selected_source"], "incremental")
        self.assertEqual(reconciliation["unsupported_terminal_item_count"], 1)

    def test_terminal_subset_selects_accumulated(self) -> None:
        sink = _Sink()
        raw = json.dumps({"path": "README.md"})
        events = [
            _msg_added(),
            _text_delta("reading"),
            _msg_done("reading"),
            _fc_added(),
            _fc_args_done(raw),
            _fc_done(raw),
            _completed([_terminal_message("reading")]),
        ]
        response = _run_stream(sink, events)
        self.assertEqual(_content_text(response), "reading")
        tool_blocks = [b for b in response["content"] if b.get("type") == "tool_use"]
        self.assertEqual(len(tool_blocks), 1)
        self.assertEqual(tool_blocks[0]["id"], "call-1")
        self.assertEqual(response["stopReason"], "tool_calls")
        reconciliation = response["provider_metadata"]["reconciliation"]
        self.assertEqual(reconciliation["selected_source"], "incremental")
        self.assertEqual(reconciliation["terminal_omission_count"], 1)

    def test_terminal_superset_selects_terminal(self) -> None:
        sink = _Sink()
        events = [
            _msg_added(),
            _text_delta("hello"),
            _msg_done("hello"),
            _completed(
                [
                    _terminal_message("hello"),
                    _terminal_message("world", item_id="msg-2"),
                ]
            ),
        ]
        response = _run_stream(sink, events)
        self.assertEqual(_content_text(response), "helloworld")
        reconciliation = response["provider_metadata"]["reconciliation"]
        self.assertEqual(reconciliation["selected_source"], "terminal")
        self.assertEqual(sink.texts("commit"), ["helloworld"])

    def test_equal_projections_select_terminal(self) -> None:
        sink = _Sink()
        events = [
            _msg_added(),
            _text_delta("same"),
            _msg_done("same"),
            _completed([_terminal_message("same")]),
        ]
        response = _run_stream(sink, events)
        self.assertEqual(_content_text(response), "same")
        reconciliation = response["provider_metadata"]["reconciliation"]
        self.assertEqual(reconciliation["selected_source"], "terminal")
        self.assertEqual(reconciliation["terminal_omission_count"], 0)

    def test_disjoint_consistent_items_commit_union(self) -> None:
        sink = _Sink()
        raw_one = json.dumps({"path": "a.py"})
        raw_two = json.dumps({"path": "b.py"})
        events = [
            _msg_added(),
            _text_delta("note"),
            _msg_done("note"),
            _fc_added(),
            _fc_args_done(raw_one),
            _fc_done(raw_one),
            _completed(
                [
                    _terminal_message("note"),
                    _terminal_function_call(raw_two, item_id="fc-2", call_id="call-2"),
                ]
            ),
        ]
        response = _run_stream(sink, events)
        tool_ids = [b["id"] for b in response["content"] if b.get("type") == "tool_use"]
        self.assertEqual(tool_ids, ["call-1", "call-2"])
        self.assertEqual(_content_text(response), "note")
        reconciliation = response["provider_metadata"]["reconciliation"]
        self.assertEqual(reconciliation["selected_source"], "union")

    def test_conflicting_text_retries_with_fresh_generation(self) -> None:
        sink = _Sink()
        calls = 0

        async def create(_params: dict[str, Any]) -> AsyncChunks:
            nonlocal calls
            calls += 1
            if calls == 1:
                return AsyncChunks(
                    [
                        _msg_added(),
                        _text_delta("hello"),
                        _msg_done("hello"),
                        _completed([_terminal_message("goodbye")]),
                    ]
                )
            return AsyncChunks(
                [
                    _msg_added(),
                    _text_delta("recovered"),
                    _msg_done("recovered"),
                    _completed([_terminal_message("recovered")]),
                ]
            )

        response = _run_stream(sink, [], max_attempts=2, create_override=create)
        self.assertEqual(_content_text(response), "recovered")
        names = [name for name, _operation in sink.operations]
        self.assertIn("supersede", names)
        self.assertEqual(names.count("commit"), 1)
        commit_op = next(op for name, op in sink.operations if name == "commit")
        self.assertEqual(commit_op["text"], "recovered")

    def test_conflicting_text_fails_after_attempt_exhaustion(self) -> None:
        sink = _Sink()
        events = [
            _msg_added(),
            _text_delta("hello"),
            _msg_done("hello"),
            _completed([_terminal_message("goodbye")]),
        ]
        with self.assertRaises(core.ProviderRequestError) as ctx:
            _run_stream(sink, events, max_attempts=1)
        record = ctx.exception.record
        self.assertEqual(record["category"], "protocol")
        self.assertEqual(sink.texts("commit"), [])

    def test_conflicting_tool_arguments_fail_without_commit(self) -> None:
        sink = _Sink()
        raw_streamed = json.dumps({"path": "a.py"})
        raw_terminal = json.dumps({"path": "b.py"})
        events = [
            _fc_added(),
            _fc_args_done(raw_streamed),
            _fc_done(raw_streamed),
            _completed([_terminal_function_call(raw_terminal)]),
        ]
        with self.assertRaises(core.ProviderRequestError) as ctx:
            _run_stream(sink, events, max_attempts=1)
        self.assertEqual(ctx.exception.record["category"], "protocol")
        self.assertEqual(sink.texts("commit"), [])
