"""Which host process owns a runtime home.

A runtime home has one supervisor daemon, and that daemon belongs to one host
process (it dies with it). Two hosts on the same home would each start a
daemon and run the same sessions twice, so the first host takes a lease on the
home and a second one is refused with the owner's pid. The lease names the
owner by pid *and* birth marker: a pid alone is recycled, a marker is not, so
a lease left behind by a crashed host is recognised as stale and reclaimed,
while a live owner that merely cannot be inspected is never overruled.

Companions that run without a daemon (a CLI that only reads the home) take no
lease; they cannot compete for supervision.
"""
from __future__ import annotations

import os
from typing import Any

from .._utils import now_iso
from ..process.identity import capture_process_birth_marker, verify_process_birth_marker
from ..storage import get_daemon_dir, read_json_file, with_record_transaction, write_json_file

LEASE_FILENAME = "owner.json"
_SCHEMA_VERSION = 1


class RuntimeOwnershipError(RuntimeError):
    """Another live process, or this one, already owns the runtime home."""

    def __init__(self, home: str, holder: dict[str, Any] | None, detail: str) -> None:
        self.home = home
        self.holder = holder or {}
        super().__init__(detail)


def owner_lease_path(home: str) -> str:
    return os.path.join(get_daemon_dir(), LEASE_FILENAME)


async def acquire_owner_lease(home: str, instance_id: str) -> dict[str, Any]:
    """Take the lease for this process, or raise naming the live holder."""
    marker = await capture_process_birth_marker()
    async with with_record_transaction("runtime-owner", home):
        path = owner_lease_path(home)
        current = await read_json_file(path, None)
        holder = current if isinstance(current, dict) and current.get("schemaVersion") == _SCHEMA_VERSION else None
        if holder is not None and holder.get("pid") != os.getpid():
            verdict = await verify_process_birth_marker(holder.get("birth"))
            if verdict == "live":
                raise RuntimeOwnershipError(
                    home,
                    holder,
                    f"Runtime home {home} is owned by a live process (pid {holder.get('pid')}, "
                    f"started {holder.get('startedAt')}); one host per runtime home.",
                )
            if verdict == "unknown":
                raise RuntimeOwnershipError(
                    home,
                    holder,
                    f"Runtime home {home} has an owner lease for pid {holder.get('pid')} that could not be "
                    f"verified as dead; not taking over. If that process is gone, remove {path}.",
                )
        lease = {
            "schemaVersion": _SCHEMA_VERSION,
            "pid": os.getpid(),
            "birth": marker,
            "instanceId": instance_id,
            "startedAt": now_iso(),
            "reclaimedFrom": None if holder is None else {"pid": holder.get("pid"), "startedAt": holder.get("startedAt")},
        }
        os.makedirs(os.path.dirname(path), exist_ok=True)
        await write_json_file(path, lease)
        return lease


async def release_owner_lease(home: str, instance_id: str) -> bool:
    """Drop the lease, but only if it is still ours."""
    async with with_record_transaction("runtime-owner", home):
        path = owner_lease_path(home)
        current = await read_json_file(path, None)
        if not isinstance(current, dict):
            return False
        if current.get("pid") != os.getpid() or current.get("instanceId") != instance_id:
            return False
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return True


async def read_owner_lease(home: str) -> dict[str, Any] | None:
    current = await read_json_file(owner_lease_path(home), None)
    return current if isinstance(current, dict) else None
