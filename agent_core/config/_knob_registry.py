from __future__ import annotations

"""Knob declarations: one row per tunable, grouped by section.

Machinery (knobValue/configureKnobs/coercion/example rendering) lives in
``_knobs.py``. Whether a constant may become a knob at all is decided by
``KNOB_CLASSIFICATION.md`` — consult it before adding a row, and before
adding a numeric constant anywhere else in the runtime.

Row shape: (name, default, kind, description, minimum, maximum) where
kind is "int" | "float" | "bool" | "str" | "int_list" and for int_list
the bounds apply to every element.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Knob:
    name: str
    default: Any
    kind: str
    description: str
    minimum: float | None = None
    maximum: float | None = None


_DECLARATIONS: tuple[tuple[str, Any, str, str, float | None, float | None], ...] = (
    ("subagents.max_live", 8, "int", "Maximum live subagent processes per runtime instance; at capacity the Agent tool returns the live-children roster instead of spawning.", 1, 64),
    ("subagents.initial_result_timeout_ms", 600000, "int", "Milliseconds the Agent tool blocks waiting for a spawned child's initial result before returning with the child still working.", 100, 3600000),
    ("subagents.resident_poll_ms", 1000, "int", "Milliseconds between a resident subagent's mailbox polls while idle between turns.", 100, 60000),
    ("provider.max_output_tokens", 16384, "int", "Fallback max output tokens per provider request when neither the request nor the provider profile specifies one.", 256, 1000000),
    ("provider.request_start_timeout_ms", 120000, "int", "Milliseconds to wait for a provider request to start streaming before the attempt is failed as timed out.", 1000, 3600000),
    ("provider.stream_idle_timeout_ms", 60000, "int", "Milliseconds of stream silence tolerated before a provider attempt is failed as idle.", 1000, 3600000),
    ("provider.json_response_timeout_ms", 120000, "int", "Milliseconds to wait for a complete non-streaming provider JSON response.", 1000, 3600000),
    ("comms.inline_result_bytes", 48000, "int", "Inline byte allowance for inter-agent payloads (SendMessage/Agent tool results and drained child messages) before artifact spill or truncation; they are conversation, not raw command output.", 4096, 512000),
    ("comms.reply_timeout_ms", 300000, "int", "Default milliseconds a SendMessage(expects_reply=true) call waits for the counterpart's reply before returning a visible non-answer.", 1000, 3600000),
    ("comms.reply_poll_ms", 500, "int", "Milliseconds between reply-mailbox polls while a SendMessage call is blocked waiting for a reply.", 50, 10000),
    ("daemon.mailbox_watch_ms", 1500, "int", "Milliseconds between the containment daemon's sweeps of live children's mailboxes for unannounced to_parent messages (the parent-wake signal).", 100, 60000),
    ("daemon.owner_poll_ms", 2000, "int", "Milliseconds between the containment daemon's liveness checks of its owner runtime process; owner death kills all owned workers and exits the daemon.", 50, 60000),
    ("daemon.heartbeat_ms", 3000, "int", "Milliseconds between the runtime's liveness checks of its containment daemon; a dead daemon is replaced by exactly one fresh instance.", 50, 60000),
    ("process.term_grace_ms", 3000, "int", "Milliseconds an owned worker process gets to exit after SIGTERM before the runtime escalates to SIGKILL.", 100, 60000),
    ("process.kill_grace_ms", 1000, "int", "Milliseconds an owned worker process gets to disappear after SIGKILL before termination is reported as failed.", 100, 60000),
    ("provider.openai_retry_delays_ms", (1000, 3000, 5000, 10000, 30000), "int_list", "Escalating retry backoff ladder in milliseconds for OpenAI-kind provider requests; attempts beyond the ladder reuse its last entry.", 0, 600000),
    ("provider.anthropic_retry_delays_ms", (350, 700), "int_list", "Quick-retry delay ladder in milliseconds for Anthropic messages requests; a small random jitter is added on top.", 0, 600000),
    ("daemon.boot_grace_ms", 300000, "int", "Milliseconds a booting daemon's not-yet-running state file is honored before the boot is treated as wedged and the instance replaced.", 1000, 3600000),
    ("daemon.start_timeout_ms", 30000, "int", "Milliseconds to wait for a starting containment daemon to accept connections before startup is failed.", 25, 600000),
    ("daemon.instance_lock_timeout_ms", 5000, "int", "Milliseconds to wait for the per-instance daemon-ensure lock before giving up on daemon startup coordination.", 100, 30000),
    ("daemon.rpc_timeout_ms", 30000, "int", "Default milliseconds a daemon RPC may run before the client fails the call; explicit per-call overrides win.", 25, 300000),
    ("daemon.auth_timeout_ms", 2000, "int", "Milliseconds a connecting client gets to complete the daemon auth handshake.", 25, 30000),
    ("daemon.events_rotate_bytes", 33554432, "int", "Byte size at which a daemon instance's events log is rotated.", 65536, 1073741824),
    ("daemon.events_tail_bytes", 65536, "int", "Bytes read from the tail of a daemon events log when loading recent events.", 4096, 16777216),
    ("daemon.events_rotate_check_ms", 300000, "int", "Milliseconds between the daemon's checks of its events log for size-based rotation.", 1000, 3600000),
    ("daemon.instance_reap_ms", 3600000, "int", "Milliseconds a dead daemon instance's on-disk directory is kept for diagnosis before the garbage collector removes it.", 60000, 86400000),
    ("daemon.close_grace_ms", 250, "int", "Milliseconds the daemon transport waits for a client connection to close cleanly during shutdown.", 25, 30000),
    ("daemon.dispatch_drain_ms", 5000, "int", "Milliseconds in-flight daemon RPC dispatches get to finish before shutdown cancels them.", 100, 300000),
    ("daemon.health_tick_ms", 2000, "int", "Milliseconds between the task supervisor's health reconcile sweeps.", 100, 300000),
    ("daemon.dead_work_grace_ms", 900000, "int", "Milliseconds a task record whose process has died keeps its last status before the boot sweep finalizes it as lost.", 1000, 86400000),
    ("daemon.live_work_cache_ttl_ms", 30000, "int", "Milliseconds the supervisor's live-work verdict cache stays fresh before process liveness is re-verified.", 0, 600000),
    ("runtime.close_failure_ms", 10000, "int", "Milliseconds runtime close waits for component cleanup before reporting the close as failed.", 500, 600000),
    ("storage.lock_timeout_ms", 5000, "int", "Default milliseconds to wait for a storage file lock before the operation fails; explicit per-call timeouts win.", 500, 60000),
    ("process.activation_timeout_ms", 30000, "int", "Milliseconds an owned worker process waits for its activation payload on stdin before exiting.", 1000, 600000),
    ("compaction.synthesis_timeout_ms", 300000, "int", "Milliseconds a compaction synthesis model call may run before it is failed.", 10000, 3600000),
    ("compaction.verification_timeout_ms", 300000, "int", "Milliseconds a compaction verification model call may run before it is failed.", 10000, 3600000),
    ("compaction.synthesis_max_retries", 3, "int", "Retries for a failed compaction synthesis call before compaction is abandoned for the turn.", 0, 10),
    ("compaction.verification_max_retries", 3, "int", "Retries for a failed compaction verification call before the unverified checkpoint path is taken.", 0, 10),
    ("compaction.evidence_token_budget", 90000, "int", "Token budget for the transcript evidence given to the compaction model.", 5000, 1000000),
    ("compaction.message_text_char_limit", 8000, "int", "Character cap per message text when building compaction evidence.", 200, 1000000),
    ("compaction.tool_input_char_limit", 2400, "int", "Character cap per tool input when building compaction evidence.", 100, 1000000),
    ("compaction.tool_result_char_limit", 8000, "int", "Character cap per tool result when building compaction evidence.", 100, 1000000),
    ("compaction.compact_tool_result_char_limit", 600, "int", "Character cap per tool result in the compact evidence fallback passes.", 50, 1000000),
    ("compaction.command_preview_char_limit", 6000, "int", "Character cap for command output previews in compaction evidence.", 100, 1000000),
    ("compaction.file_preview_lines", 48, "int", "Line cap for file previews in compaction evidence.", 4, 10000),
    ("compaction.match_preview_limit", 32, "int", "Maximum search matches quoted in compaction evidence.", 1, 10000),
    ("compaction.min_recent_messages", 20, "int", "Minimum recent messages always kept out of compaction.", 2, 1000),
    ("compaction.raw_tail_token_target", 100000, "int", "Token size of the raw transcript tail compaction aims to preserve.", 1000, 10000000),
    ("compaction.raw_tail_token_budget", 40000, "int", "Token budget for the raw tail retained alongside a checkpoint.", 1000, 10000000),
    ("compaction.working_context_token_limit", 200000, "int", "Token ceiling for the working-context document compaction maintains.", 10000, 10000000),
    ("compaction.max_preserved_tool_pairs", 10, "int", "Maximum recent tool exchange pairs preserved verbatim across compaction.", 3, 100),
    ("compaction.preserved_trace_token_budget", 20000, "int", "Token budget for a preserved agentic-trace record carried across compaction.", 1000, 1000000),
    ("memory.max_records", 48, "int", "Maximum durable memory records persisted per project.", 1, 10000),
    ("memory.max_text_chars", 480, "int", "Character cap per durable memory record.", 40, 100000),
    ("memory.retrieved_records", 8, "int", "Maximum durable memory records retrieved into a session's context.", 1, 1000),
    ("memory.retrieved_token_budget", 1600, "int", "Token budget for retrieved durable memory in a session's context.", 100, 1000000),
    ("context.raw_checkpoint_token_limit", 200000, "int", "Raw-context token level at which a durable checkpoint is considered.", 10000, 10000000),
    ("context.estimate_margin_tokens", 2000, "int", "Safety margin tokens added to estimated request input sizes.", 0, 100000),
    ("context.relief_preserve_min_messages", 12, "int", "Minimum messages always preserved when context-relief trimming activates.", 2, 1000),
    ("context.relief_raw_tail_tokens", 24000, "int", "Preferred raw-tail token size when context relief rebuilds the transcript.", 1000, 10000000),
    ("context.relief_message_text_chars", 1200, "int", "Character cap per message text under context relief.", 100, 1000000),
    ("context.relief_tool_input_chars", 500, "int", "Character cap per tool input under context relief.", 50, 1000000),
    ("context.relief_tool_result_chars", 750, "int", "Character cap per tool result under context relief.", 50, 1000000),
    ("context.relief_context_token_limit", 200000, "int", "Context token ceiling that triggers relief handling.", 10000, 10000000),
)


def _declare() -> dict[str, Knob]:
    registry: dict[str, Knob] = {}
    for name, default, kind, description, minimum, maximum in _DECLARATIONS:
        if name in registry:
            raise ValueError(f"Duplicate knob declaration: {name}")
        registry[name] = Knob(name, default, kind, description, minimum, maximum)
    return registry


KNOB_REGISTRY: dict[str, Knob] = _declare()
