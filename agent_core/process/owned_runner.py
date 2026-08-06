from __future__ import annotations

import json
import os
import re
import select
import signal
import subprocess
import sys
from typing import Any


MAX_ARGV_ITEMS = 4096
MAX_ARGV_BYTES = 512 * 1024
ACTIVATION_TIMEOUT_SECONDS = 30
FORWARDED_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGQUIT)


def main() -> None:
    launch = _parse_launch(sys.argv[1] if len(sys.argv) > 1 else None)
    child: subprocess.Popen[Any] | None = None
    pending_signal: int | None = None

    def forward(signum: int, _frame: Any) -> None:
        nonlocal pending_signal
        pending_signal = pending_signal or signum
        if child is not None and child.poll() is None:
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass
            return
        _reraise(signum)

    previous_handlers = {signum: signal.signal(signum, forward) for signum in FORWARDED_SIGNALS}
    try:
        token = launch.get("activationToken")
        if token is not None:
            _wait_for_activation(token)
        child = subprocess.Popen(
            launch["argv"],
            cwd=os.getcwd(),
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
        )
        code = child.wait()
        if pending_signal is not None:
            _restore_handlers(previous_handlers)
            _reraise(pending_signal)
        if code < 0:
            _restore_handlers(previous_handlers)
            _reraise(-code)
        raise SystemExit(code)
    finally:
        _restore_handlers(previous_handlers)


def _parse_launch(raw: str | None) -> dict[str, Any]:
    if raw is None or len(raw.encode("utf-8")) > MAX_ARGV_BYTES:
        raise ValueError("owned-process-runner argv is missing or exceeds its byte limit")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("owned-process-runner argv is not valid JSON") from exc
    argv = parsed if isinstance(parsed, list) else parsed.get("argv") if isinstance(parsed, dict) else None
    if (
        not isinstance(argv, list)
        or not argv
        or len(argv) > MAX_ARGV_ITEMS
        or not all(isinstance(value, str) and "\0" not in value for value in argv)
    ):
        raise ValueError("owned-process-runner argv is invalid")
    token = None if isinstance(parsed, list) else parsed.get("activationToken")
    if token is not None and (not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token)):
        raise ValueError("owned-process-runner activation token is invalid")
    return {"argv": argv, "activationToken": token}


def _wait_for_activation(expected_token: str) -> None:
    ready, _, _ = select.select([sys.stdin.buffer], [], [], ACTIVATION_TIMEOUT_SECONDS)
    if not ready:
        raise TimeoutError("Owned process runner activation timed out.")
    raw = sys.stdin.buffer.readline(256)
    if raw != f"activate:{expected_token}\n".encode("ascii"):
        raise RuntimeError("Owned process runner activation token did not match.")


def _restore_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def _reraise(signum: int) -> None:
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        if isinstance(exc, SystemExit):
            raise
        print(str(exc), file=sys.stderr, flush=True)
        raise
