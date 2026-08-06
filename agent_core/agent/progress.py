from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from .._utils import now_iso
from ..session.message_origin import syntheticUserMessage


PROGRESS_LEDGER_SCHEMA_VERSION = 1
REPEATED_TOOL_CYCLE_LIMIT = 3
ARTIFACT_ACQUISITION_FAILURE_LIMIT = 3
GENERAL_NO_PROGRESS_LIMIT = 12
MAX_RECORDED_FINGERPRINTS = 64
MAX_RECORDED_ARTIFACTS = 32
MAX_REPEATED_CYCLE_BATCHES = 8

_VOLATILE_KEYS = {
    "callid",
    "createdat",
    "duration",
    "durationms",
    "elapsed",
    "elapsedms",
    "finishedat",
    "heartbeat",
    "messageid",
    "occurredat",
    "requestid",
    "retry",
    "startedat",
    "timestamp",
    "tooluseid",
    "updatedat",
}
_ARTIFACT_POINTER_KEYS = {"artifactid", "filepath"}


class LogicalTurnProgressLedger:
    """Bounded, durable progress evidence for one logical user turn."""

    def __init__(self, session: dict[str, Any], logical_turn_id: str) -> None:
        self.session = session
        self.logical_turn_id = str(logical_turn_id)
        metadata = session.setdefault("metadata", {})
        existing = metadata.get("turnProgress")
        if (
            isinstance(existing, dict)
            and existing.get("schemaVersion") == PROGRESS_LEDGER_SCHEMA_VERSION
            and existing.get("logicalTurnId") == self.logical_turn_id
        ):
            self.receipt = existing
        else:
            self.receipt = {
                "schemaVersion": PROGRESS_LEDGER_SCHEMA_VERSION,
                "logicalTurnId": self.logical_turn_id,
                "providerCallIndex": 0,
                "toolBatchIndex": 0,
                "consecutiveNoProgress": 0,
                "batchOccurrences": {},
                "recentBatchFingerprints": [],
                "artifactOffsets": {},
                "artifactFailures": {},
                "lastProgressReason": None,
                "terminalReason": None,
                "updatedAt": now_iso(),
            }
            metadata["turnProgress"] = self.receipt

    @property
    def terminal_reason(self) -> str | None:
        reason = self.receipt.get("terminalReason")
        return str(reason) if reason else None

    def terminalResult(self) -> dict[str, Any] | None:
        reason = self.terminal_reason
        return self._terminal_result(reason) if reason else None

    def complete(self, *, provider_call_index: int) -> None:
        self.receipt["providerCallIndex"] = int(provider_call_index)
        self.receipt["completed"] = True
        self.receipt["completionReason"] = "provider_response"
        self.receipt["updatedAt"] = now_iso()

    def observe(
        self,
        tool_uses: list[dict[str, Any]],
        tool_results: list[dict[str, Any] | None],
        *,
        provider_call_index: int,
    ) -> dict[str, Any] | None:
        if self.terminal_reason:
            return self._terminal_result(self.terminal_reason)

        batch_fingerprint = _batch_fingerprint(tool_uses, tool_results)
        occurrences = self.receipt.setdefault("batchOccurrences", {})
        prior_occurrences = int(occurrences.get(batch_fingerprint) or 0)
        total_occurrences = prior_occurrences + 1
        occurrences[batch_fingerprint] = total_occurrences
        consecutive_occurrences = (
            int(self.receipt.get("lastBatchOccurrenceCount") or 0) + 1
            if self.receipt.get("lastBatchFingerprint") == batch_fingerprint
            else 1
        )
        recent = self.receipt.setdefault("recentBatchFingerprints", [])
        recent.append(batch_fingerprint)
        if len(recent) > MAX_RECORDED_FINGERPRINTS:
            del recent[:-MAX_RECORDED_FINGERPRINTS]
        _prune_occurrences(occurrences, recent)
        repeated_cycle = _suffix_cycle_evidence(recent)
        if repeated_cycle is None:
            self.receipt.pop("repeatedCycleCount", None)
            self.receipt.pop("lastCycleLength", None)
        else:
            self.receipt["repeatedCycleCount"] = repeated_cycle["occurrences"]
            self.receipt["lastCycleLength"] = repeated_cycle["length"]

        artifact_observation = self._observe_artifact_pages(tool_uses, tool_results)
        batch_is_novel = prior_occurrences == 0
        progressed = batch_is_novel or artifact_observation["advanced"]
        if progressed:
            self.receipt["consecutiveNoProgress"] = 0
            self.receipt["lastProgressReason"] = (
                "artifact_cursor_advanced"
                if artifact_observation["advanced"]
                else "novel_reachable_evidence"
            )
        else:
            self.receipt["consecutiveNoProgress"] = (
                int(self.receipt.get("consecutiveNoProgress") or 0) + 1
            )

        self.receipt["providerCallIndex"] = int(provider_call_index)
        self.receipt["toolBatchIndex"] = (
            int(self.receipt.get("toolBatchIndex") or 0) + 1
        )
        self.receipt["lastBatchFingerprint"] = batch_fingerprint
        self.receipt["lastBatchOccurrenceCount"] = consecutive_occurrences
        self.receipt["lastBatchTotalOccurrences"] = total_occurrences
        self.receipt["updatedAt"] = now_iso()

        terminal_reason: str | None = None
        diagnostic: dict[str, Any] = {}
        if artifact_observation["failed"]:
            terminal_reason = "artifact_output_unreachable"
            diagnostic["artifactId"] = artifact_observation["artifactId"]
        elif (
            repeated_cycle is not None
            and repeated_cycle["occurrences"] >= REPEATED_TOOL_CYCLE_LIMIT
        ):
            terminal_reason = "repeated_tool_cycle"
            diagnostic["cycleFingerprint"] = repeated_cycle["fingerprint"]
        elif (
            int(self.receipt.get("consecutiveNoProgress") or 0)
            >= GENERAL_NO_PROGRESS_LIMIT
        ):
            terminal_reason = "no_semantic_progress"
            diagnostic["cycleFingerprint"] = batch_fingerprint

        if terminal_reason is None:
            return None
        self.receipt["terminalReason"] = terminal_reason
        self.receipt["terminalDiagnostic"] = diagnostic
        self.receipt["updatedAt"] = now_iso()
        return self._terminal_result(terminal_reason, diagnostic)

    def _observe_artifact_pages(
        self,
        tool_uses: list[dict[str, Any]],
        tool_results: list[dict[str, Any] | None],
    ) -> dict[str, Any]:
        offsets = self.receipt.setdefault("artifactOffsets", {})
        failures = self.receipt.setdefault("artifactFailures", {})
        advanced = False
        failed_artifact: str | None = None

        for index, tool_use in enumerate(tool_uses):
            if tool_use.get("name") != "ReadToolArtifact":
                continue
            block = tool_results[index] if index < len(tool_results) else None
            payload = _json_object(block.get("content") if isinstance(block, dict) else None)
            input_value = tool_use.get("input")
            tool_input = input_value if isinstance(input_value, dict) else {}
            artifact_id = str(
                (payload or {}).get("artifactId")
                or tool_input.get("artifact_id")
                or ""
            )
            if not artifact_id:
                continue
            highest = int(offsets.get(artifact_id) or 0)
            next_offset = _safe_nonnegative_int((payload or {}).get("nextOffsetBytes"))
            page_type = (payload or {}).get("type")
            complete = bool((payload or {}).get("complete"))
            page_advanced = (
                page_type == "tool_artifact_page"
                and next_offset is not None
                and (
                    next_offset > highest
                    or (complete and artifact_id not in offsets)
                )
            )
            if page_advanced:
                offsets[artifact_id] = max(highest, int(next_offset or 0))
                failures[artifact_id] = 0
                advanced = True
            else:
                failures[artifact_id] = int(failures.get(artifact_id) or 0) + 1
                if failures[artifact_id] >= ARTIFACT_ACQUISITION_FAILURE_LIMIT:
                    failed_artifact = artifact_id

        _prune_mapping(offsets, MAX_RECORDED_ARTIFACTS)
        _prune_mapping(failures, MAX_RECORDED_ARTIFACTS)
        return {
            "advanced": advanced,
            "failed": failed_artifact is not None,
            "artifactId": failed_artifact,
        }

    def _terminal_result(
        self,
        reason: str,
        diagnostic: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "reason": reason,
            **(diagnostic or self.receipt.get("terminalDiagnostic") or {}),
            "providerCallIndex": int(self.receipt.get("providerCallIndex") or 0),
            "toolBatchIndex": int(self.receipt.get("toolBatchIndex") or 0),
            "consecutiveNoProgress": int(
                self.receipt.get("consecutiveNoProgress") or 0
            ),
        }


def noProgressEventData(
    *,
    session_id: str,
    logical_turn_id: str,
    receipt: dict[str, Any],
    recovered: bool = False,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "logical_turn_id": logical_turn_id,
        **({"recovered_terminal": True} if recovered else {}),
        **receipt,
    }


def noProgressTurnResult(
    *,
    receipt: dict[str, Any],
    text: str,
    tool_count: int,
    usage: dict[str, Any],
    model: str,
    estimated_cost_usd: float,
) -> dict[str, Any]:
    return {
        "text": text,
        "toolCount": tool_count,
        "usage": usage,
        "model": model,
        "estimatedCostUsd": estimated_cost_usd,
        "stopReason": "no_progress",
        "noProgress": receipt,
    }


def appendToolResultBatch(
    *,
    session: dict[str, Any],
    ledger: LogicalTurnProgressLedger,
    logical_turn_id: str,
    tool_uses: list[dict[str, Any]],
    tool_results: list[dict[str, Any] | None],
    provider_call_index: int,
) -> dict[str, Any] | None:
    session["messages"].append(
        syntheticUserMessage(
            [result for result in tool_results if result is not None],
            actor="tool",
            kind="tool_result_batch",
            lineage={"logicalTurnId": logical_turn_id},
        )
    )
    return ledger.observe(
        tool_uses,
        tool_results,
        provider_call_index=provider_call_index,
    )


def _batch_fingerprint(
    tool_uses: list[dict[str, Any]],
    tool_results: list[dict[str, Any] | None],
) -> str:
    batch: list[dict[str, Any]] = []
    for index, tool_use in enumerate(tool_uses):
        result = tool_results[index] if index < len(tool_results) else None
        batch.append(
            {
                "tool": str(tool_use.get("name") or ""),
                "input": _normalize(tool_use.get("input") or {}),
                "result": _normalize_result(result),
            }
        )
    encoded = json.dumps(
        batch,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _normalize_result(result: dict[str, Any] | None) -> Any:
    if not isinstance(result, dict):
        return None
    content = result.get("content")
    parsed = _json_object(content)
    return {
        "content": _normalize(parsed if parsed is not None else content),
        "isError": bool(result.get("is_error") or result.get("isError")),
    }


def _normalize(value: Any, *, parent_key: str | None = None) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, child in sorted(value.items(), key=lambda item: str(item[0])):
            normalized_key = str(key)
            comparable_key = normalized_key.replace("_", "").lower()
            if comparable_key in _VOLATILE_KEYS:
                continue
            if (
                parent_key == "artifact"
                and comparable_key in _ARTIFACT_POINTER_KEYS
            ):
                continue
            normalized[normalized_key] = _normalize(
                child,
                parent_key=comparable_key,
            )
        return normalized
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [_normalize(child, parent_key=parent_key) for child in value]
    if isinstance(value, bytes):
        return {"sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
    if isinstance(value, float):
        return round(value, 9)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    return str(value)


def _json_object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _safe_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _suffix_cycle_evidence(recent: list[str]) -> dict[str, Any] | None:
    """Return a bounded repeated suffix pattern without matching old revisits."""

    best: dict[str, Any] | None = None
    maximum_length = min(MAX_REPEATED_CYCLE_BATCHES, len(recent) // 2)
    for length in range(1, maximum_length + 1):
        pattern = recent[-length:]
        occurrences = 1
        cursor = len(recent) - (2 * length)
        while cursor >= 0 and recent[cursor : cursor + length] == pattern:
            occurrences += 1
            cursor -= length
        if occurrences < 2:
            continue
        if best is not None and occurrences <= int(best["occurrences"]):
            continue
        fingerprint = (
            pattern[0]
            if length == 1
            else hashlib.sha256(
                json.dumps(pattern, separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:24]
        )
        best = {
            "length": length,
            "occurrences": occurrences,
            "fingerprint": fingerprint,
        }
    return best


def _prune_occurrences(
    occurrences: dict[str, Any],
    recent: list[str],
) -> None:
    retained = set(recent)
    for key in list(occurrences):
        if key not in retained:
            occurrences.pop(key, None)


def _prune_mapping(mapping: dict[str, Any], maximum: int) -> None:
    while len(mapping) > maximum:
        mapping.pop(next(iter(mapping)))
