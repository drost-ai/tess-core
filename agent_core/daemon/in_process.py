"""Subscription parity for the in-process supervisor fallback.

When the daemon is unavailable the supervisor runs in-process; this shim
patches its subscribe() so event replay and bootstrap snapshots behave
exactly like a daemon-backed subscription."""
from __future__ import annotations

import asyncio
from typing import Any

from .._utils import maybe_await
from ..storage import loadDaemonEvents
from .client import _normalise_subscribe_args
from .events import (
    _collaboration_snapshot_payload,
    _event_matches_session,
    _make_snapshot_record,
    _monitor_snapshot_payload,
    _task_snapshot_payload,
)

def _install_in_process_subscription_parity(instance_id: str, supervisor: dict[str, Any]) -> None:
    live_subscribe = supervisor["subscribe"]

    def subscribe(*args: Any, **kwargs: Any):
        listener, session_id, after_cursor = _normalise_subscribe_args(args, kwargs)
        unsubscribe_live = live_subscribe(listener, session_id)
        release_attachment = supervisor["addAttachment"](session_id)
        replay_task: asyncio.Task[Any] | None = None

        async def replay() -> None:
            records = await loadDaemonEvents(instance_id)
            for record in records:
                cursor = record.get("cursor")
                if isinstance(cursor, int) and cursor <= (after_cursor or 0):
                    continue
                if _event_matches_session(record, session_id):
                    await maybe_await(listener(record))
            if not session_id:
                return
            bootstrap = await supervisor["getBootstrap"](session_id)
            next_cursor = max(
                [record.get("cursor", 0) for record in records if isinstance(record.get("cursor"), int)],
                default=0,
            )
            snapshots = (
                ("task.snapshot", _task_snapshot_payload(session_id, bootstrap.get("taskSnapshot") or {})),
                ("monitor.snapshot", _monitor_snapshot_payload(session_id, bootstrap.get("monitorSnapshot") or {})),
                ("collaboration.snapshot", _collaboration_snapshot_payload(bootstrap.get("collaborationSnapshot") or {})),
            )
            for event_name, data in snapshots:
                next_cursor += 1
                await maybe_await(
                    listener(
                        _make_snapshot_record(
                            next_cursor,
                            session_id,
                            event_name,
                            data,
                        )
                    )
                )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(replay())
        else:
            replay_task = loop.create_task(replay())
        unsubscribed = False

        def unsubscribe() -> None:
            nonlocal unsubscribed
            if unsubscribed:
                return
            unsubscribed = True
            if replay_task is not None and not replay_task.done():
                replay_task.cancel()
            unsubscribe_live()
            release_attachment()

        return unsubscribe

    supervisor["subscribe"] = subscribe
