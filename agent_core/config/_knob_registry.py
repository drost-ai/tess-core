from __future__ import annotations

"""Knob declarations.

Split from ``_knobs.py`` (which keeps the machinery: knobValue,
configureKnobs, coercion, example rendering) so the declaration table can
grow past the per-file line budget. One ``Knob`` per tunable, grouped by
section. Classification rules for what may become a knob live in
``KNOB_CLASSIFICATION.md`` next to this module — consult it before adding
a constant anywhere in the runtime.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Knob:
    name: str
    default: Any
    kind: str  # "int" | "float" | "bool" | "str" | "int_list"
    description: str
    minimum: float | None = None  # for int_list: applies to every element
    maximum: float | None = None


def _declare(*knobs: Knob) -> dict[str, Knob]:
    registry: dict[str, Knob] = {}
    for knob in knobs:
        if knob.name in registry:
            raise ValueError(f"Duplicate knob declaration: {knob.name}")
        registry[knob.name] = knob
    return registry


KNOB_REGISTRY: dict[str, Knob] = _declare(
    Knob(
        "subagents.max_live",
        8,
        "int",
        "Maximum live subagent processes per runtime instance; at capacity the Agent tool returns the live-children roster instead of spawning.",
        minimum=1,
        maximum=64,
    ),
    Knob(
        "subagents.initial_result_timeout_ms",
        600_000,
        "int",
        "Milliseconds the Agent tool blocks waiting for a spawned child's initial result before returning with the child still working.",
        minimum=100,
        maximum=3_600_000,
    ),
    Knob(
        "subagents.resident_poll_ms",
        1_000,
        "int",
        "Milliseconds between a resident subagent's mailbox polls while idle between turns.",
        minimum=100,
        maximum=60_000,
    ),
    Knob(
        "provider.max_output_tokens",
        16384,
        "int",
        "Fallback max output tokens per provider request when neither the request nor the provider profile specifies one.",
        minimum=256,
        maximum=1_000_000,
    ),
    Knob(
        "provider.request_start_timeout_ms",
        120_000,
        "int",
        "Milliseconds to wait for a provider request to start streaming before the attempt is failed as timed out.",
        minimum=1_000,
        maximum=3_600_000,
    ),
    Knob(
        "provider.stream_idle_timeout_ms",
        60_000,
        "int",
        "Milliseconds of stream silence tolerated before a provider attempt is failed as idle.",
        minimum=1_000,
        maximum=3_600_000,
    ),
    Knob(
        "provider.json_response_timeout_ms",
        120_000,
        "int",
        "Milliseconds to wait for a complete non-streaming provider JSON response.",
        minimum=1_000,
        maximum=3_600_000,
    ),
    Knob(
        "comms.inline_result_bytes",
        48_000,
        "int",
        "Inline byte allowance for inter-agent payloads (SendMessage/Agent tool results and drained child messages) before artifact spill or truncation; they are conversation, not raw command output.",
        minimum=4_096,
        maximum=512_000,
    ),
    Knob(
        "comms.reply_timeout_ms",
        300_000,
        "int",
        "Default milliseconds a SendMessage(expects_reply=true) call waits for the counterpart's reply before returning a visible non-answer.",
        minimum=1_000,
        maximum=3_600_000,
    ),
    Knob(
        "comms.reply_poll_ms",
        500,
        "int",
        "Milliseconds between reply-mailbox polls while a SendMessage call is blocked waiting for a reply.",
        minimum=50,
        maximum=10_000,
    ),
    Knob(
        "daemon.mailbox_watch_ms",
        1_500,
        "int",
        "Milliseconds between the containment daemon's sweeps of live children's mailboxes for unannounced to_parent messages (the parent-wake signal).",
        minimum=100,
        maximum=60_000,
    ),
    Knob(
        "daemon.owner_poll_ms",
        2_000,
        "int",
        "Milliseconds between the containment daemon's liveness checks of its owner runtime process; owner death kills all owned workers and exits the daemon.",
        minimum=50,
        maximum=60_000,
    ),
    Knob(
        "daemon.heartbeat_ms",
        3_000,
        "int",
        "Milliseconds between the runtime's liveness checks of its containment daemon; a dead daemon is replaced by exactly one fresh instance.",
        minimum=50,
        maximum=60_000,
    ),
    Knob(
        "process.term_grace_ms",
        3_000,
        "int",
        "Milliseconds an owned worker process gets to exit after SIGTERM before the runtime escalates to SIGKILL.",
        minimum=100,
        maximum=60_000,
    ),
    Knob(
        "process.kill_grace_ms",
        1_000,
        "int",
        "Milliseconds an owned worker process gets to disappear after SIGKILL before termination is reported as failed.",
        minimum=100,
        maximum=60_000,
    ),
    Knob(
        "provider.openai_retry_delays_ms",
        (1_000, 3_000, 5_000, 10_000, 30_000),
        "int_list",
        "Escalating retry backoff ladder in milliseconds for OpenAI-kind provider requests; attempts beyond the ladder reuse its last entry.",
        minimum=0,
        maximum=600_000,
    ),
    Knob(
        "provider.anthropic_retry_delays_ms",
        (350, 700),
        "int_list",
        "Quick-retry delay ladder in milliseconds for Anthropic messages requests; a small random jitter is added on top.",
        minimum=0,
        maximum=600_000,
    ),
    Knob(
        "daemon.boot_grace_ms",
        300_000,
        "int",
        "Milliseconds a booting daemon's not-yet-running state file is honored before the boot is treated as wedged and the instance replaced.",
        minimum=1_000,
        maximum=3_600_000,
    ),
    Knob(
        "daemon.start_timeout_ms",
        30_000,
        "int",
        "Milliseconds to wait for a starting containment daemon to accept connections before startup is failed.",
        minimum=25,
        maximum=600_000,
    ),
    Knob(
        "daemon.instance_lock_timeout_ms",
        5_000,
        "int",
        "Milliseconds to wait for the per-instance daemon-ensure lock before giving up on daemon startup coordination.",
        minimum=100,
        maximum=30_000,
    ),
    Knob(
        "daemon.rpc_timeout_ms",
        30_000,
        "int",
        "Default milliseconds a daemon RPC may run before the client fails the call; explicit per-call overrides win.",
        minimum=25,
        maximum=300_000,
    ),
    Knob(
        "daemon.auth_timeout_ms",
        2_000,
        "int",
        "Milliseconds a connecting client gets to complete the daemon auth handshake.",
        minimum=25,
        maximum=30_000,
    ),
    Knob(
        "daemon.events_rotate_bytes",
        32 * 1024 * 1024,
        "int",
        "Byte size at which a daemon instance's events log is rotated.",
        minimum=64 * 1024,
        maximum=1024 * 1024 * 1024,
    ),
    Knob(
        "daemon.events_tail_bytes",
        64 * 1024,
        "int",
        "Bytes read from the tail of a daemon events log when loading recent events.",
        minimum=4_096,
        maximum=16 * 1024 * 1024,
    ),
    Knob(
        "daemon.events_rotate_check_ms",
        300_000,
        "int",
        "Milliseconds between the daemon's checks of its events log for size-based rotation.",
        minimum=1_000,
        maximum=3_600_000,
    ),
    Knob(
        "daemon.instance_reap_ms",
        3_600_000,
        "int",
        "Milliseconds a dead daemon instance's on-disk directory is kept for diagnosis before the garbage collector removes it.",
        minimum=60_000,
        maximum=86_400_000,
    ),
    Knob(
        "daemon.close_grace_ms",
        250,
        "int",
        "Milliseconds the daemon transport waits for a client connection to close cleanly during shutdown.",
        minimum=25,
        maximum=30_000,
    ),
    Knob(
        "daemon.dispatch_drain_ms",
        5_000,
        "int",
        "Milliseconds in-flight daemon RPC dispatches get to finish before shutdown cancels them.",
        minimum=100,
        maximum=300_000,
    ),
    Knob(
        "daemon.health_tick_ms",
        2_000,
        "int",
        "Milliseconds between the task supervisor's health reconcile sweeps.",
        minimum=100,
        maximum=300_000,
    ),
    Knob(
        "daemon.dead_work_grace_ms",
        900_000,
        "int",
        "Milliseconds a task record whose process has died keeps its last status before the boot sweep finalizes it as lost.",
        minimum=1_000,
        maximum=86_400_000,
    ),
    Knob(
        "daemon.live_work_cache_ttl_ms",
        30_000,
        "int",
        "Milliseconds the supervisor's live-work verdict cache stays fresh before process liveness is re-verified.",
        minimum=0,
        maximum=600_000,
    ),
    Knob(
        "runtime.close_failure_ms",
        10_000,
        "int",
        "Milliseconds runtime close waits for component cleanup before reporting the close as failed.",
        minimum=500,
        maximum=600_000,
    ),
    Knob(
        "storage.lock_timeout_ms",
        5_000,
        "int",
        "Default milliseconds to wait for a storage file lock before the operation fails; explicit per-call timeouts win.",
        minimum=500,
        maximum=60_000,
    ),
    Knob(
        "process.activation_timeout_ms",
        30_000,
        "int",
        "Milliseconds an owned worker process waits for its activation payload on stdin before exiting.",
        minimum=1_000,
        maximum=600_000,
    ),
)
