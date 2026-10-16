"""POSIX stdio group leader that survives its client's abrupt death.

Executed by file path, with only stdlib imports. The private pipe's write end
belongs exclusively to the client; EOF is ownership loss, not a PID heuristic.
Stdio passes straight through to the server. Servers must not deliberately
escape their process group (this is lifecycle containment, not a sandbox).
"""
from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time


def main() -> None:
    owner_fd = int(sys.argv[1])
    status_fd = int(sys.argv[2])
    grace = max(0.01, float(sys.argv[3]))
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stop)
    # Never signal a group unless this dedicated wrapper is its leader.
    if os.getpgrp() != os.getpid():
        raise RuntimeError("MCP guardian must own its process group")
    child = None
    try:
        # Owner may have died before this interpreter finished starting.
        if not select.select([owner_fd], [], [], 0)[0] and not stopping:
            child = subprocess.Popen(sys.argv[4:], close_fds=True)
            while not stopping and child.poll() is None:
                if select.select([owner_fd], [], [], 0.05)[0]:
                    break
    finally:
        if child is not None and child.poll() is not None:
            try:
                os.write(status_fd, str(child.returncode).encode("ascii"))
            except BrokenPipeError:
                pass
        os.close(status_fd)
        os.close(owner_fd)
        # Retain the group leader until escalation: its PID cannot be reused
        # between TERM and KILL, even if the server exits first.
        os.killpg(os.getpid(), signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if child is not None:
                child.poll()  # Reap our direct child when possible.
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        os.killpg(os.getpid(), signal.SIGKILL)


if __name__ == "__main__":
    main()
