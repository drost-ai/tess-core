from __future__ import annotations

from typing import Any

from .subprocess_output import SUBPROCESS_SPOOL_MAX_BYTES
from .tool_payloads import shapeTextToolResult
from .utf8_bounds import sliceUtf8Prefix


async def buildBashResultPayload(
    args: dict[str, Any] | None = None, **kwargs: Any
) -> dict[str, Any]:
    values = {**(args or {}), **kwargs}
    run = values["run"]
    stdout = await _shape_captured_stream(values, "stdout")
    stderr = await _shape_captured_stream(values, "stderr")
    output_bytes = run["stdoutBytes"] + run["stderrBytes"]
    output_available = (
        "artifact"
        if run.get("stdoutArtifact") or run.get("stderrArtifact")
        else "partial"
        if output_bytes > 0
        else "none"
    )
    common = {
        "type": "bash_result",
        "command": sliceUtf8Prefix(values["command"], 2_048),
        "cwd": sliceUtf8Prefix(values["cwd"], 1_024),
        "exitCode": run.get("exitCode"),
        "exitSignal": run.get("exitSignal"),
        "stdout": stdout["text"],
        "stderr": stderr["text"],
        "stdout_manifest": stdout["view"],
        "stderr_manifest": stderr["view"],
        "outputAvailable": output_available,
        "measuredBytes": output_bytes,
        "spoolBytes": run["spoolBytes"],
        "ringBytes": run["ringBytes"],
    }
    if run["outputLimitExceeded"]:
        return {
            "payload": {
                **common,
                "status": "terminated",
                "reason": "output_limit",
                "retryable": False,
                "sideEffectsMayHaveOccurred": True,
                "outputTruncated": True,
                "limitBytes": values.get("spoolLimitBytes") or SUBPROCESS_SPOOL_MAX_BYTES,
                **({"artifactUnavailable": True} if run["artifactUnavailable"] else {}),
                "note": (
                    "The command was terminated after reaching the output acquisition limit. "
                    "Mutations completed before termination may remain."
                ),
            },
            "isError": True,
            "summaryText": f"{stdout['text']}{stderr['text']}",
        }
    is_error = bool(
        run["timedOut"]
        or run.get("exitCode")
        or run["backgroundDescendantsDetected"]
    )
    persistence_failed_after_output = bool(run["artifactUnavailable"] and output_bytes > 0)
    return {
        "payload": {
            **common,
            "status": "terminated" if run["timedOut"] else "failed" if is_error else "completed",
            "timedOut": run["timedOut"],
            **(
                {
                    "artifactUnavailable": True,
                    "note": (
                        "Command completed; output view persistence failed. "
                        "The inline head/tail view is bounded."
                    ),
                }
                if persistence_failed_after_output
                else {}
            ),
            **(
                {
                    **({"stdioLeakDetected": True} if run["stdioLeakDetected"] else {}),
                    "backgroundDescendantsDetected": True,
                    "backgroundDescendantsTerminated": run["backgroundDescendantsTerminated"],
                    "diagnostic": run.get("diagnostic"),
                }
                if run["backgroundDescendantsDetected"]
                else {}
            ),
        },
        "isError": is_error,
        "summaryText": f"{stdout['text']}{stderr['text']}",
    }


async def _shape_captured_stream(values: dict[str, Any], kind: str) -> dict[str, Any]:
    run = values["run"]
    byte_count = run[f"{kind}Bytes"]
    return await shapeTextToolResult(
        config=values["config"],
        sessionId=values["sessionId"],
        toolName="Bash",
        family="bash",
        text=run[kind],
        originalBytes=byte_count,
        originalLines=run[f"{kind}Lines"] + (1 if byte_count > 0 else 0),
        existingArtifact=run.get(f"{kind}Artifact"),
        artifactUnavailable=run["artifactUnavailable"],
        persistArtifact=False,
        sourceTruncated=bool(run["outputLimitExceeded"] or byte_count > len(run[kind].encode("utf-8"))),
        strategy="head_tail",
    )


build_bash_result_payload = buildBashResultPayload
