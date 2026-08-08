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
)
