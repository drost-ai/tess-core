from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from ..bash_lifecycle import runInlineShellCommand
from ..bounded_web_fetch import boundedWebFetch
from ..utf8_bounds import sliceUtf8Prefix
from .health_check_policy import (
    assertHealthCheckExecutionPolicy,
    buildHealthCheckEnvironment,
    healthCheckSpecDigest,
)


HEALTH_LOG_MAX_BYTES = 4 * 1024 * 1024
HEALTH_LOG_ROTATIONS = 3
HEALTH_EVENT_EXCERPT_BYTES = 4 * 1024


async def executeHealthCheck(
    *,
    record: dict[str, Any],
    signal: Any,
    onProcessIdentity: Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    policy = record.get("executionPolicy")
    if record.get("schemaVersion") != 2 or not record.get("specDigest") or not isinstance(policy, dict):
        raise RuntimeError("Health check has no verified execution policy.")
    spec = recordToSpec(record)
    if healthCheckSpecDigest(spec) != record["specDigest"]:
        raise RuntimeError("Health-check persisted spec no longer matches its authorized digest.")
    assertHealthCheckExecutionPolicy(spec, policy)
    if record["mode"] == "command":
        return await _execute_command(record, signal, onProcessIdentity)
    return await _execute_http(record, signal)


def recordToSpec(record: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "title",
        "mode",
        "cwd",
        "intervalMs",
        "timeoutMs",
        "failureThreshold",
        "backoffMultiplier",
        "maxIntervalMs",
        "outputLimitBytes",
        "bodyLimitBytes",
        "command",
        "url",
        "expectedStatusCodes",
        "expectedBodyPolicy",
        "expectedBodySubstring",
    )
    return {key: record[key] for key in keys if record.get(key) is not None}


async def appendHealthCheckLog(
    record: dict[str, Any], startedAt: str, result: dict[str, Any]
) -> None:
    target = record.get("outputFile")
    if not isinstance(target, str) or not target:
        return
    await _rotate_log(target)
    status = "aborted" if result["aborted"] else "ok" if result["okay"] else "failed"
    line = (
        f"[{startedAt}] status={status} measured_bytes={result['measuredBytes']} "
        f"truncated={str(bool(result['outputTruncated'])).lower()}\n"
        f"{result['outputExcerpt']}\n"
    )
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(sliceUtf8Prefix(line, HEALTH_EVENT_EXCERPT_BYTES + 512))


async def _execute_command(
    record: dict[str, Any],
    signal: Any,
    on_identity: Callable[[dict[str, Any]], Any] | None,
) -> dict[str, Any]:
    policy = record["executionPolicy"]
    shell = policy.get("shellPath")
    if not record.get("command") or not record.get("cwd") or not shell:
        raise RuntimeError("Command health check is missing its normalized command policy.")
    prefix = (policy.get("sandbox") or {}).get("argvPrefix") or []
    argv = [*prefix, shell, "-lc", record["command"]]
    run = await runInlineShellCommand(
        shell=shell,
        command=record["command"],
        argv=argv,
        cwd=record["cwd"],
        env=buildHealthCheckEnvironment(policy),
        timeoutMs=record["timeoutMs"],
        abortSignal=signal,
        sessionId=record["sessionId"],
        toolName="TaskHealthCheck",
        maxSpoolBytes=record["outputLimitBytes"],
        maxRingBytes=min(record["outputLimitBytes"], 32 * 1024),
        onProcessIdentity=on_identity,
    )
    output = f"{run['stdout']}{run['stderr']}".strip()
    body_ok = record.get("expectedBodyPolicy") != "contains" or str(record.get("expectedBodySubstring") or "") in output
    okay = run["exitCode"] == 0 and not run["timedOut"] and not run["aborted"] and not run["outputLimitExceeded"] and body_ok
    if okay:
        error = None
    elif run["aborted"]:
        error = "Health check was cancelled."
    elif run["timedOut"]:
        error = f"Health check exceeded its {record['timeoutMs']}ms deadline."
    elif run["outputLimitExceeded"]:
        error = f"Health check output exceeded {record['outputLimitBytes']} bytes."
    elif run["exitCode"] != 0:
        error = f"Health check exited with code {run['exitCode']}."
    else:
        error = f"Command output did not contain expected text: {record.get('expectedBodySubstring')}"
    return _shape(
        okay=okay,
        aborted=bool(run["aborted"]),
        output=output,
        outputTruncated=bool(run["outputLimitExceeded"]),
        lastError=error,
        lastExitCode=run["exitCode"],
        lastSignal=run.get("exitSignal"),
        lastTimedOut=bool(run["timedOut"]),
        lastTerminated=bool(run.get("exitSignal")),
        processIdentity=run.get("processIdentity"),
        outputArtifacts=[item for item in (run.get("stdoutArtifact"), run.get("stderrArtifact")) if item],
        measuredBytes=int(run.get("stdoutBytes") or 0) + int(run.get("stderrBytes") or 0),
    )


async def _execute_http(record: dict[str, Any], signal: Any) -> dict[str, Any]:
    policy = record["executionPolicy"]
    expected = record.get("expectedStatusCodes")
    if not record.get("url") or not isinstance(expected, list):
        raise RuntimeError("HTTP health check is missing its normalized network policy.")
    allowed = set(policy.get("allowedOrigins") or [])

    async def authorize(url: str) -> None:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if not (policy.get("sandbox") or {}).get("network") or origin not in allowed:
            raise RuntimeError("Health-check redirect exceeds its approved network authority.")

    response = await boundedWebFetch(
        url=record["url"],
        abortSignal=signal,
        compressedMaxBytes=record["bodyLimitBytes"],
        decodedMaxBytes=record["bodyLimitBytes"],
        connectTimeoutMs=min(record["timeoutMs"], 10_000),
        headersTimeoutMs=min(record["timeoutMs"], 10_000),
        bodyIdleTimeoutMs=min(record["timeoutMs"], 10_000),
        totalTimeoutMs=record["timeoutMs"],
        maxRedirects=policy["maxRedirects"],
        acceptedStatusCodes=expected,
        authorizeHop=authorize,
    )
    body = str(response.get("body") or "")
    body_ok = record.get("expectedBodyPolicy") != "contains" or str(record.get("expectedBodySubstring") or "") in body
    status_ok = isinstance(response.get("statusCode"), int) and response["statusCode"] in expected
    okay = response.get("outcome") == "ok" and status_ok and body_ok
    if okay:
        error = None
    elif response.get("truncated"):
        error = f"HTTP health-check acquisition stopped at {response.get('reason') or 'a configured limit'}."
    elif not status_ok:
        error = f"Unexpected status code {response.get('statusCode', 'unknown')}."
    else:
        error = f"Response body did not contain expected text: {record.get('expectedBodySubstring')}"
    return _shape(
        okay=okay,
        aborted=_aborted(signal),
        output=f"HTTP {response.get('statusCode', 'unknown')}{chr(10) + body if body else ''}",
        outputTruncated=bool(response.get("truncated")),
        lastError=error,
        lastStatusCode=response.get("statusCode"),
        lastTimedOut=False,
        lastTerminated=False,
        responseSnippet=sliceUtf8Prefix(body, HEALTH_EVENT_EXCERPT_BYTES),
        measuredBytes=max(int(response.get("compressedBytes") or 0), int(response.get("decodedBytes") or 0)),
    )


def _shape(**values: Any) -> dict[str, Any]:
    result = {key: value for key, value in values.items() if value is not None}
    result["outputExcerpt"] = sliceUtf8Prefix(
        str(result.get("output") or result.get("lastError") or "(no output)"),
        HEALTH_EVENT_EXCERPT_BYTES,
    )
    return result


async def _rotate_log(target: str) -> None:
    path = Path(target)
    try:
        if path.stat().st_size < HEALTH_LOG_MAX_BYTES:
            return
    except OSError:
        return
    try:
        Path(f"{target}.{HEALTH_LOG_ROTATIONS}").unlink()
    except OSError:
        pass
    for index in range(HEALTH_LOG_ROTATIONS - 1, 0, -1):
        try:
            os.replace(f"{target}.{index}", f"{target}.{index + 1}")
        except OSError:
            pass
    try:
        os.replace(target, f"{target}.1")
    except OSError:
        pass


def _aborted(signal: Any) -> bool:
    if hasattr(signal, "is_set"):
        return bool(signal.is_set())
    return bool(getattr(signal, "aborted", False))


execute_health_check = executeHealthCheck
