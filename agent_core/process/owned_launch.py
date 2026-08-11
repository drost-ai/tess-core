from __future__ import annotations

import json
import re
import subprocess
import sys
import uuid
from typing import Any


def create_owned_process_runner_argv(
    childArgv: list[str],
    *,
    activationToken: str | None = None,
) -> list[str]:
    payload: dict[str, Any] = {"argv": childArgv}
    if activationToken is not None:
        payload["activationToken"] = activationToken
    return [
        sys.executable,
        "-m",
        "agent_core.process.owned_runner",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    ]


def create_owned_process_activation(childArgv: list[str]) -> dict[str, Any]:
    token = uuid.uuid4().hex
    return {
        "activationToken": token,
        "argv": create_owned_process_runner_argv(childArgv, activationToken=token),
    }


def activate_owned_process_runner(child: subprocess.Popen[Any], activationToken: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", activationToken):
        raise ValueError("Owned process runner activation token is invalid.")
    if child.stdin is None or child.poll() is not None:
        raise RuntimeError("Owned process runner activation channel is unavailable.")
    try:
        child.stdin.write(f"activate:{activationToken}\n".encode("ascii"))
        child.stdin.flush()
    finally:
        child.stdin.close()


