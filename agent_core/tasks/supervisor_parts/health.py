from __future__ import annotations

from .shared import *


async def _inspect_latest_task_record(task_id: str) -> dict[str, Any] | None:
    """Prefer the newest health projection while its three stores converge."""

    record = await loadTaskRecord(task_id)
    meta = await readTaskMeta(task_id)
    if (record or {}).get("kind") == "health_check" or (meta or {}).get("type") == "health_check":
        health = await loadHealthCheckTask(task_id)
        latest = max(
            (item for item in (record, meta, health) if isinstance(item, dict)),
            key=lambda item: str(item.get("updatedAt") or ""),
            default=None,
        )
        if latest is not record:
            return _build_task_record(latest, None)
    return record or (_build_task_record(meta, None) if meta else None)


async def _run_health_check(record: dict[str, Any], emit: Callable[[str, dict[str, Any], str | None], Any]) -> None:
    task_id = record["taskId"]
    while True:
        current = await loadHealthCheckTask(task_id)
        if not current or current.get("status") not in {"active", "unhealthy", "running"}:
            return
        previous_status = current.get("status")
        result = await _execute_health_check(current)
        now = now_iso()
        failure_threshold = max(1, int(current.get("failureThreshold") or 2))
        consecutive_failures = 0 if result.get("ok") else int(current.get("consecutiveFailures") or 0) + 1
        next_status = "active" if result.get("ok") else "failed" if consecutive_failures >= failure_threshold else "unhealthy"
        updates = {
            "lastRunAt": now,
            "lastResult": result,
            "updatedAt": now,
            "status": next_status,
            "consecutiveFailures": consecutive_failures,
            "currentIntervalMs": int(current.get("intervalMs") or 10000) if result.get("ok") else _next_backoff_interval_ms(current, consecutive_failures),
            "lastSuccessAt": now if result.get("ok") else current.get("lastSuccessAt"),
            "lastFailureAt": current.get("lastFailureAt") if result.get("ok") else now,
            "lastError": None if result.get("ok") else result.get("error") or result.get("summary"),
            "lastExitCode": result.get("exitCode", current.get("lastExitCode")),
            "lastStatusCode": result.get("status", current.get("lastStatusCode")),
            "lastResponseSnippet": result.get("snippet", current.get("lastResponseSnippet")),
            "lastSignal": result.get("signal", current.get("lastSignal")),
            "lastTimedOut": bool(result.get("timedOut")),
            "lastTerminated": bool(result.get("terminated")),
        }
        output_file = current.get("outputFile") or taskOutputPath(task_id)
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        with open(output_file, "a", encoding="utf-8") as handle:
            handle.write(f"[{now}] {result.get('output') or result.get('summary') or ''}\n")

        declined: list[bool] = [False]

        def _apply_tick(latest: dict[str, Any] | None) -> dict[str, Any] | None:
            if latest is not None and latest.get("status") not in {"active", "unhealthy", "running"}:
                # A concurrent stop won between this tick's check and its
                # write; keep the stop's record instead of resurrecting the
                # check (persisting `latest` unchanged is a no-op rewrite).
                declined[0] = True
                return latest
            return {**(latest or current), **updates, "outputFile": output_file}

        merged = await mutateHealthCheckTask(task_id, _apply_tick)
        if merged is None or declined[0]:
            return
        current = merged
        await writeTaskMeta(task_id, current)
        await saveTaskRecord(_build_task_record(current, result.get("summary")))
        if next_status == "unhealthy":
            await maybe_await(
                emit(
                    "health_check.unhealthy",
                    {"task_id": task_id, "title": current.get("title"), "failures": consecutive_failures, "error": current.get("lastError")},
                    current.get("sessionId"),
                )
            )
        elif previous_status != "active" and next_status == "active":
            await maybe_await(emit("health_check.recovered", {"task_id": task_id, "title": current.get("title")}, current.get("sessionId")))
        if next_status == "failed":
            return
        await asyncio.sleep(max(0.25, int(current.get("currentIntervalMs") or current.get("intervalMs") or 10000) / 1000))


async def _execute_health_check(record: dict[str, Any]) -> dict[str, Any]:
    timeout = max(1, int(record.get("timeoutMs") or 5000) / 1000)
    if record.get("mode") == "command":
        command = record.get("command")
        if not command:
            return {"ok": False, "summary": "No health-check command configured."}
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=record.get("cwd") or os.getcwd(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            signal_name = "SIGTERM"
            with contextlib.suppress(Exception):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 1)
            except asyncio.TimeoutError:
                signal_name = "SIGKILL"
                with contextlib.suppress(Exception):
                    proc.kill()
                await proc.wait()
            await proc.wait()
            return {
                "ok": False,
                "summary": "Health check timed out.",
                "output": "Health check timed out.",
                "error": "Health check timed out.",
                "exitCode": None,
                "signal": signal_name,
                "timedOut": True,
                "terminated": True,
            }
        output = (stdout + stderr).decode("utf-8", errors="replace").strip()
        expected_body = record.get("expectedBodySubstring")
        body_ok = expected_body in output if expected_body else True
        ok = proc.returncode == 0 and body_ok
        error = None if ok else f"Health check exited with code {proc.returncode}" if proc.returncode != 0 else f"Command output did not contain expected text: {expected_body}"
        return {
            "ok": ok,
            "summary": output[-500:] if output else f"exit {proc.returncode}",
            "output": output,
            "snippet": output[:500],
            "exitCode": proc.returncode,
            "error": error,
        }
    import urllib.request
    import urllib.error

    url = record.get("url")
    if not url:
        return {"ok": False, "summary": "No health-check URL configured.", "error": "No health-check URL configured."}

    def fetch() -> dict[str, Any]:
        try:
            with urllib.request.urlopen(str(url), timeout=timeout) as response:
                body = response.read(4096).decode("utf-8", errors="replace")
                return {"status": response.status, "reason": getattr(response, "reason", ""), "body": body}
        except urllib.error.HTTPError as exc:
            body = exc.read(4096).decode("utf-8", errors="replace")
            return {"status": exc.code, "reason": getattr(exc, "reason", ""), "body": body}

    try:
        response = await asyncio.to_thread(fetch)
    except Exception as exc:
        return {"ok": False, "summary": str(exc), "output": str(exc), "error": str(exc)}
    expected = record.get("expectedStatusCodes") or [200]
    ok = response["status"] in expected
    expected_body = record.get("expectedBodySubstring")
    if expected_body:
        ok = ok and expected_body in response["body"]
    error = None
    if not ok:
        error = f"Unexpected status code {response['status']}" if response["status"] not in expected else f"Response body did not contain expected text: {expected_body}"
    reason = f" {response['reason']}" if response.get("reason") else ""
    output = f"HTTP {response['status']}{reason}{chr(10) + response['body'][:500] if response.get('body') else ''}"
    return {"ok": ok, "summary": f"HTTP {response['status']}", "output": output, "status": response["status"], "snippet": response.get("body", "")[:500], "error": error}


def _next_backoff_interval_ms(record: dict[str, Any], failure_count: int) -> int:
    base = max(250, int(record.get("intervalMs") or 10000))
    multiplier = max(1, float(record.get("backoffMultiplier") or 2))
    max_interval = max(base, int(record.get("maxIntervalMs") or base * 4))
    return min(max_interval, round(base * (multiplier ** max(0, failure_count - 1))))
