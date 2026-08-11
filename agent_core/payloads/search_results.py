from __future__ import annotations
from .._utils import drop_none_deep as _without_none

import os
from typing import Any

from .cursor import encode_payload_cursor
from .utf8_bounds import utf8_byte_length


def build_streaming_glob_payload(**kwargs: Any) -> dict[str, Any]:
    common = _build_common_payload(type="glob", **kwargs)
    counts: dict[str, int] = {}
    for match in kwargs["result"]["matches"]:
        directory = os.path.dirname(match) or "(root)"
        counts[directory] = counts.get(directory, 0) + 1
    directory_counts = [
        {"directory": name, "count": count}
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:12]
    ]
    return {
        **common,
        "ignoredDefaultsApplied": True,
        "directoryCounts": directory_counts,
        "directoryCountsScope": "returned_sample",
    }


def build_streaming_grep_payload(**kwargs: Any) -> dict[str, Any]:
    return {
        **_build_common_payload(type="grep", **kwargs),
        "outputMode": kwargs["outputMode"],
        "headLimit": kwargs["limit"],
    }


def _build_common_payload(
    *,
    type: str,
    pattern: str,
    searchPath: str,
    offset: int,
    limit: int,
    result: dict[str, Any],
    sourceFingerprint: str,
    **_kwargs: Any,
) -> dict[str, Any]:
    acquisition_incomplete = bool(
        result["outputLimitExceeded"] or result["timedOut"]
    )
    next_record_offset = offset + result["pageRecordsAdvanced"]
    has_more = next_record_offset < result["totalMatches"]
    truncated = bool(
        acquisition_incomplete
        or has_more
        or result["fragmentedRecords"]
        or result["skippedRecords"]
        or result["oversizedRecords"]
    )
    artifact = result.get("stdoutArtifact")
    next_offset = next_record_offset if not acquisition_incomplete and has_more else None
    next_cursor = (
        encode_payload_cursor(
            family=type,  # type: ignore[arg-type]
            sourceFingerprint=sourceFingerprint,
            position={
                "recordOffset": next_offset,
                "snapshotFingerprint": result["resultFingerprint"],
            },
        )
        if next_offset is not None
        else None
    )
    payload = {
        "type": type,
        "status": "partial" if acquisition_incomplete else "completed",
        "pattern": pattern,
        "path": searchPath,
        "matches": result["matches"],
        "totalMatches": result["totalMatches"],
        "totalMatchesExact": not acquisition_incomplete,
        "observedMatches": result["totalMatches"],
        "offset": offset,
        "limit": limit,
        "returnedMatches": len(result["matches"]),
        "advancedMatches": result["pageRecordsAdvanced"],
        "resultFingerprint": result["resultFingerprint"],
        "skippedRecords": result["skippedRecords"] or None,
        "fragmentedRecords": result["fragmentedRecords"] or None,
        "skippedRemainderPolicy": (
            "Each returned record fragment advances past the remainder; use the artifact for full output."
            if result["fragmentedRecords"]
            else "One record could not fit the minimum inline page and was explicitly skipped."
            if result["skippedRecords"]
            else None
        ),
        "truncated": truncated,
        "nextCursor": next_cursor,
        "nextOffset": next_offset,
        "oversizedRecords": result["oversizedRecords"] or None,
        "artifact": artifact,
        "artifactUnavailable": (
            True if truncated and not artifact and result["artifactUnavailable"] else None
        ),
        "acquisition": (
            {
                "status": "terminated",
                "reason": (
                    "output_limit" if result["outputLimitExceeded"] else "timeout"
                ),
                "measuredBytes": result["stdoutBytes"] + result["stderrBytes"],
                "spoolBytes": result["spoolBytes"],
                "processGroupDrained": result["processGroupDrained"],
            }
            if acquisition_incomplete
            else None
        ),
        "transcriptView": {
            "strategy": "count_and_sample" if truncated else "inline_full",
            "truncated": truncated,
            "originalBytes": result["stdoutBytes"],
            "originalLines": result["totalMatches"],
            "returnedBytes": utf8_byte_length("\n".join(result["matches"])),
            "returnedLines": len(result["matches"]),
            "artifact": artifact,
        },
    }
    return _without_none(payload)


