from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import tess_core._facade as core

from .fake_providers import FakeProviderRuntime
from .paths import REPO_ROOT
from .runtime_bindings import bind_agent_runtime

CORPUS_SCHEMA = "tess-core.event-corpus.v1"
DEFAULT_CORPUS_DIR = REPO_ROOT / "fixtures" / "event-corpus"


def exportEventCorpus(output_dir: str | Path = DEFAULT_CORPUS_DIR) -> dict[str, Any]:
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    contracts = _contracts()
    files = []
    for contract in contracts:
        name = f"{contract['id']}.json"
        _write_json(target / name, contract)
        files.append(name)
    manifest = {
        "schemaVersion": CORPUS_SCHEMA,
        "generator": "python -m tess_core_harness export-event-corpus",
        "contractCount": len(contracts),
        "contracts": files,
        "consumerAcknowledgements": [],
        "consumerStatus": "unacknowledged",
    }
    _write_json(target / "manifest.json", manifest)
    return manifest


def verifyEventCorpus(corpus_dir: str | Path = DEFAULT_CORPUS_DIR) -> dict[str, Any]:
    expected = Path(corpus_dir)
    with tempfile.TemporaryDirectory(prefix="tess-core-event-corpus-") as tmp:
        generated = Path(tmp)
        exportEventCorpus(generated)
        expected_files = sorted(path.name for path in expected.glob("*.json"))
        generated_files = sorted(path.name for path in generated.glob("*.json"))
        mismatches = []
        if expected_files != generated_files:
            mismatches.append("file set differs")
        for name in sorted(set(expected_files) & set(generated_files)):
            if (expected / name).read_bytes() != (generated / name).read_bytes():
                mismatches.append(name)
    return {
        "schemaVersion": CORPUS_SCHEMA,
        "status": "passed" if not mismatches else "failed",
        "byteIdentical": not mismatches,
        "mismatches": mismatches,
        "contractCount": len(generated_files) - 1,
    }


def _contracts() -> list[dict[str, Any]]:
    captured = asyncio.run(_capture_isolated_events())
    return [
        _contract(
            "preflight-admission",
            captured["preflight"],
            "isolated:run_agent_prompt/preflight",
        ),
        _contract(
            "partial-output",
            captured["partial"],
            "isolated:FakeProviderRuntime/openai-partial",
        ),
        _contract(
            "recovery-retry",
            captured["recovery"],
            "isolated:FakeProviderRuntime/openai-recovery",
        ),
        _contract(
            "no-progress-ledger",
            captured["no_progress"],
            "isolated:run_agent_prompt/no-progress",
        ),
        _contract(
            "result-guard",
            captured["result_guard"],
            "isolated:run_agent_prompt/result-guard",
        ),
        _contract(
            "turn-sessions",
            captured["turn_sessions"],
            "isolated:create_session_runtime_controller/cancelled+completed",
        ),
    ]


def _contract(identifier: str, events: list[dict[str, Any]], source: str) -> dict[str, Any]:
    return {
        "schemaVersion": CORPUS_SCHEMA,
        "id": identifier,
        "source": source,
        "events": [
            {
                "ordinal": index,
                "event": event["event"],
                "dataKeys": sorted((event.get("data") or {}).keys()),
                "typedKind": (event.get("typedEvent") or {}).get("kind"),
                "payloadKeys": sorted(((event.get("typedEvent") or {}).get("payload") or {}).keys()),
                "scenario": event.get("scenario"),
            }
            for index, event in enumerate(events)
        ],
    }


def _runtime_event(event: str, data: dict[str, Any], *, scenario: str) -> dict[str, Any]:
    typed = core.create_runtime_event({"event": event, "data": data, "createdAt": "<timestamp>"})
    return {"event": event, "data": data, "typedEvent": typed, "scenario": scenario}


async def _capture_isolated_events() -> dict[str, list[dict[str, Any]]]:
    return {
        "preflight": await _capture_agent_loop_scenario("preflight"),
        "partial": await _capture_provider_partial(),
        "recovery": await _capture_provider_recovery(),
        "no_progress": await _capture_agent_loop_scenario("no_progress"),
        "result_guard": await _capture_agent_loop_scenario("result_guard"),
        "turn_sessions": _capture_turn_sessions(),
    }


async def _capture_agent_loop_scenario(kind: str) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix=f"tess-core-event-{kind}-") as tmp:
        root = Path(tmp)
        home = root / "home"
        workspace = root / "workspace"
        home.mkdir()
        workspace.mkdir()
        async with core.scoped_runtime_home(str(home)):
            session = core.create_empty_session(
                cwd=str(workspace),
                provider="openai",
                model="gpt-5.4",
                systemPrompt="isolated event corpus",
                permissionMode="bypassPermissions",
                sessionId=f"event-{kind}",
            )
            profile = {
                "name": "event-corpus",
                "kind": "openai",
                "apiMode": "responses",
                "maxContextLength": 100_000,
                "maxOutputTokens": 4_096,
            }
            config = {
                "cwd": str(workspace),
                "runtimeHome": str(home),
                "allowedRoots": [str(workspace)],
                "agents": {},
                "provider": "openai",
                "providerKind": "openai",
                "providerProfileName": "event-corpus",
                "providerProfile": profile,
                "providerProfiles": {"event-corpus": profile},
                "customHeaders": {},
                "openaiApiMode": "responses",
                "model": "gpt-5.4",
                "effort": "medium",
                "assistantName": "Harness",
                "assistantDescription": "",
                "systemPrompt": "isolated event corpus",
                "tools": ["probe"] if kind != "preflight" else [],
                "allowedTools": ["probe"] if kind != "preflight" else [],
                "disallowedTools": [],
                "permissionMode": "bypassPermissions",
                "sessionPersistence": False,
            }
            raw_events: list[tuple[str, dict[str, Any]]] = []
            calls = 0

            async def probe(_input: dict[str, Any], _context: Any) -> dict[str, Any]:
                content = "same evidence" if kind == "no_progress" else "large result " * 4_000
                return {"content": content}

            tool = core.ToolDefinition(
                "probe",
                "Produce deterministic evidence.",
                {"type": "object", "properties": {"value": {"type": "integer"}}},
                probe,
            )

            async def send(_request: dict[str, Any]) -> dict[str, Any]:
                nonlocal calls
                calls += 1
                if kind == "preflight" or (kind == "result_guard" and calls > 1):
                    return _provider_text_response("done")
                return {
                    "model": "gpt-5.4",
                    "stopReason": "tool_use",
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"probe-{calls}",
                            "name": "probe",
                            "input": {"value": 1},
                        }
                    ],
                }

            runtime = bind_agent_runtime(
                session,
                {
                    "tools": [] if kind == "preflight" else [tool],
                    "emitEvent": lambda event, data: raw_events.append((event, data)),
                },
            )
            await core.run_agent_prompt(
                session=session,
                config=config,
                runtime=runtime,
                prompt=f"capture {kind}",
                providerOverride={"send": send},
            )
            target = {
                "preflight": {"provider.request_preflight"},
                "no_progress": {"agent.no_progress"},
                "result_guard": {"tool.result_guard"},
            }[kind]
            selected = [
                _runtime_event(event, data, scenario=kind)
                for event, data in raw_events
                if event in target
            ]
            if not selected:
                raise RuntimeError(f"Isolated {kind} scenario did not emit its contract event")
            return selected


async def _capture_provider_partial() -> list[dict[str, Any]]:
    runtime = FakeProviderRuntime()
    try:
        await runtime.run(
            {
                "provider": "openai",
                "api": "responses",
                "runtime": {"streamProviderMessages": True, "includePartialMessages": True},
                "script": {
                    "attempts": [
                        {
                            "stream": [
                                {"event": {"type": "response.output_text.delta", "delta": "partial"}},
                                {
                                    "event": {
                                        "type": "response.completed",
                                        "response": {
                                            "output": [
                                                {
                                                    "type": "message",
                                                    "content": [{"type": "output_text", "text": "partial"}],
                                                }
                                            ],
                                            "usage": {"input_tokens": 2, "output_tokens": 1},
                                        },
                                    }
                                },
                            ]
                        }
                    ]
                },
            }
        )
        return _typed_provider_events(runtime.events, {"message.assistant.partial"}, "partial")
    finally:
        runtime.close()


async def _capture_provider_recovery() -> list[dict[str, Any]]:
    runtime = FakeProviderRuntime()
    try:
        await runtime.run(
            {
                "provider": "openai",
                "api": "responses",
                "maxAttempts": 2,
                "runtime": {"streamProviderMessages": True},
                "script": {
                    "attempts": [
                        {"createError": "socket hang up"},
                        {
                            "stream": [
                                {
                                    "event": {
                                        "type": "response.completed",
                                        "response": {
                                            "output": [
                                                {
                                                    "type": "message",
                                                    "content": [{"type": "output_text", "text": "recovered"}],
                                                }
                                            ],
                                            "usage": {"input_tokens": 2, "output_tokens": 1},
                                        },
                                    }
                                }
                            ]
                        },
                    ]
                },
            }
        )
        return _typed_provider_events(runtime.events, {"provider.retrying", "provider.recovered"}, "recovery")
    finally:
        runtime.close()


def _typed_provider_events(
    events: list[dict[str, Any]], names: set[str], scenario: str
) -> list[dict[str, Any]]:
    selected = [
        _runtime_event(item["event"], item["data"], scenario=scenario)
        for item in events
        if item["event"] in names
    ]
    if not selected:
        raise RuntimeError(f"Isolated {scenario} provider scenario emitted no target events")
    return selected


def _capture_turn_sessions() -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    def controller(scenario: str) -> dict[str, Any]:
        return core.create_session_runtime_controller(
            session={"id": f"turn-{scenario}", "model": "gpt-5.4"},
            config={},
            emit=lambda event, data: captured.append(
                _runtime_event(event, data, scenario=scenario)
            ),
        )

    cancelled = controller("cancelled-turn")
    cancelled["startTurn"]({"prompt": "cancel me"})
    cancelled["setTurnCancelling"]()
    cancelled["finishTurn"]({"kind": "cancelled", "stopReason": "cancelled"})
    completed = controller("completed-turn")
    completed["startTurn"]({"prompt": "complete me"})
    completed["finishTurn"]({"kind": "completed", "stopReason": "end_turn"})
    return [event for event in captured if event["event"].startswith("turn.")]


def _provider_text_response(text: str) -> dict[str, Any]:
    return {
        "model": "gpt-5.4",
        "stopReason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 2},
        "content": [{"type": "text", "text": text}],
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")


export_event_corpus = exportEventCorpus
verify_event_corpus = verifyEventCorpus
