from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import agent_core as core

from .paths import DEFAULT_GOLDENS_DIR, REPO_ROOT

CORPUS_SCHEMA = "agent-core.event-corpus.v1"
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
        "generator": "python -m agent_core_harness export-event-corpus",
        "contractCount": len(contracts),
        "contracts": files,
        "consumptionTargets": [
            {"port": "typescript", "package": "25", "gate": "3"},
            {"port": "rust", "package": "16", "gate": "3"},
            {"port": "go", "package": "10", "gate": "2"},
        ],
    }
    _write_json(target / "manifest.json", manifest)
    return manifest


def verifyEventCorpus(corpus_dir: str | Path = DEFAULT_CORPUS_DIR) -> dict[str, Any]:
    expected = Path(corpus_dir)
    with tempfile.TemporaryDirectory(prefix="agent-core-event-corpus-") as tmp:
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
    provider_events = _read_events(
        DEFAULT_GOLDENS_DIR / "providers.fake-provider-pack" / "families" / "provider_events.json"
    )
    runtime_events = _read_events(
        DEFAULT_GOLDENS_DIR / "runtime.runtime-tool-pack" / "families" / "runtime_events.json"
    )
    return [
        _contract(
            "preflight-admission",
            _select(runtime_events, {"provider.request_preflight"}, limit=1),
            "runtime.runtime-tool-pack/provider.request_preflight",
        ),
        _contract(
            "partial-output",
            _select(provider_events, {"provider.stream_event", "message.assistant.partial"}),
            "providers.fake-provider-pack/openai-responses",
        ),
        _contract(
            "recovery-retry",
            _select(provider_events, {"provider.request_started", "provider.retrying", "provider.recovered"}),
            "providers.fake-provider-pack/retry-attempt-lineage",
        ),
        _contract(
            "no-progress-ledger",
            [_runtime_event("agent.no_progress", _no_progress_data())],
            "agent_core.agent_loop.progress/noProgressEventData",
        ),
        _contract(
            "result-budgets",
            [_runtime_event("tool.result_budget", _result_budget_data())],
            "agent_core.agent_loop.tool_result_budget/ToolResultBatchBudget.record",
        ),
        _contract(
            "turn-sessions",
            [
                _runtime_event("turn.started", {"session_id": "session", "prompt": "work"}),
                _runtime_event("turn.cancelling", {"session_id": "session"}),
                _runtime_event("turn.cancelled", {"session_id": "session", "partial_text": "partial"}),
                _runtime_event("turn.completed", {"session_id": "session", "stop_reason": "end_turn", "model": "model"}),
            ],
            "agent_core.session_runtime/SessionRuntime",
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
            }
            for index, event in enumerate(events)
        ],
    }


def _read_events(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError(f"Event source must be a list: {path}")
    return [item for item in value if isinstance(item, dict) and isinstance(item.get("event"), str)]


def _select(events: list[dict[str, Any]], names: set[str], *, limit: int | None = None) -> list[dict[str, Any]]:
    selected = [event for event in events if event["event"] in names]
    return selected[:limit] if limit is not None else selected


def _runtime_event(event: str, data: dict[str, Any]) -> dict[str, Any]:
    typed = core.createRuntimeEvent({"event": event, "data": data, "createdAt": "<timestamp>"})
    return {"event": event, "data": data, "typedEvent": typed}


def _no_progress_data() -> dict[str, Any]:
    return {
        "session_id": "session",
        "logical_turn_id": "turn",
        "reason": "repeated_tool_cycle",
        "artifact_id": "artifact",
        "cycle_fingerprint": "fingerprint",
        "provider_call_index": 4,
        "tool_batch_index": 3,
        "consecutive_no_progress": 3,
        "recovered_terminal": False,
    }


def _result_budget_data() -> dict[str, Any]:
    return {
        "session_id": "session",
        "effective_result_budget_bytes": 12000,
        "batch_budget_bytes": 24000,
        "original_bytes": 40000,
        "final_bytes": 24000,
        "truncated_result_count": 1,
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")


export_event_corpus = exportEventCorpus
verify_event_corpus = verifyEventCorpus
