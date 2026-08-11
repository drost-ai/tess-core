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
    ("provider.token_count_timeout_ms", 15_000, "int", "Default milliseconds for a provider token-count request; per-profile tokenCountTimeoutMs and per-call options win.", 1_000, 600_000),
    ("provider.total_request_timeout_ms", 600_000, "int", "Default milliseconds for a whole provider request; per-profile totalRequestTimeoutMs and per-call options win.", 10_000, 7_200_000),
    ("provider.max_context_length", 200_000, "int", "Default provider context length in tokens when the profile does not declare maxContextLength and no model rule infers one.", 8_000, 100_000_000),
    ("provider.compaction_threshold_tokens", 200_000, "int", "Default token level that triggers compaction when the profile does not declare compactionThresholdTokens.", 10_000, 100_000_000),
    ("provider.compaction_preservation_ratio", 0.1, "float", "Fraction of the source transcript compaction aims to preserve verbatim.", 0.01, 0.9),
    ("provider.compaction_headroom_ratio", 0.25, "float", "Fraction of the compaction threshold reserved as output headroom for the compaction model.", 0.01, 0.9),
    ("provider.output_budget_safety_margin_tokens", 4_096, "int", "Safety margin subtracted from the provider output budget when sizing requests.", 0, 1_000_000),
    ("provider.codex_inline_tool_result_batch_bytes", 24_000, "int", "Byte budget per inline tool-result batch on codex-style responses transports.", 1_024, 10_000_000),
    ("provider.openai_max_attempts", 6, "int", "Default attempts per OpenAI-kind request before the turn fails; per-call maxAttempts wins.", 1, 20),
    ("provider.anthropic_max_attempts", 2, "int", "Default attempts per Anthropic request before the turn fails; per-call maxAttempts wins.", 1, 20),
    ("provider.partial_output_ack_timeout_ms", 5_000, "int", "Milliseconds to wait for a partial-output sink acknowledgement before proceeding without it.", 100, 600_000),
    ("tools.artifact_page_bytes", 16_384, "int", "Default bytes per ReadToolArtifact page when the call does not specify max_bytes.", 256, 24_000),
    ("tools.diff_context_lines", 3, "int", "Unchanged context lines around each hunk in file-change diffs.", 0, 100),
    ("tools.search_timeout_ms", 120_000, "int", "Default milliseconds a streaming code search may run; per-call timeouts win.", 1_000, 3_600_000),
    ("payloads.file_change_result_max_bytes", 12_000, "int", "Byte cap for a file-change tool result payload before its diff is trimmed.", 1_000, 10_000_000),
    ("payloads.file_change_diff_max_lines", 120, "int", "Line cap for the inline diff in a file-change tool result.", 10, 100_000),
    ("payloads.file_change_diff_target_bytes", 8_000, "int", "Byte target the file-change diff is trimmed toward when the payload is over budget.", 500, 10_000_000),
    ("payloads.notebook_cells_per_page", 20, "int", "Notebook cells per NotebookRead page.", 1, 1_000),
    ("payloads.notebook_cell_source_bytes", 2_000, "int", "Byte cap per notebook cell source in a NotebookRead page.", 100, 10_000_000),
    ("payloads.notebook_output_sample_bytes", 600, "int", "Byte cap per sampled notebook cell output.", 50, 10_000_000),
    ("payloads.non_artifact_tool_result_max_bytes", 32_000, "int", "Byte ceiling for tool results of families that never spill to artifacts.", 4_096, 512_000),
    ("images.max_count", 8, "int", "Default images accepted per request when the provider profile is silent; bounded by the hardcoded provider ceilings.", 1, 32),
    ("images.max_bytes", 5 * 1024 * 1024, "int", "Default byte cap per input image when the provider profile is silent.", 64 * 1024, 20 * 1024 * 1024),
    ("images.max_aggregate_bytes", 20 * 1024 * 1024, "int", "Default byte cap for all input images together when the provider profile is silent.", 64 * 1024, 40 * 1024 * 1024),
    ("images.max_encoded_request_bytes", 24 * 1024 * 1024, "int", "Default byte cap for the encoded request carrying image inputs when the provider profile is silent.", 64 * 1024, 56 * 1024 * 1024),
    ("web.fetch_connect_timeout_ms", 10_000, "int", "Milliseconds to establish a WebFetch connection; per-call overrides win.", 500, 600_000),
    ("web.fetch_headers_timeout_ms", 10_000, "int", "Milliseconds to receive WebFetch response headers; per-call overrides win.", 500, 600_000),
    ("web.fetch_body_idle_timeout_ms", 10_000, "int", "Milliseconds of WebFetch body silence tolerated before the fetch fails; per-call overrides win.", 500, 600_000),
    ("web.fetch_total_timeout_ms", 30_000, "int", "Total milliseconds for one WebFetch acquisition; per-call overrides win.", 1_000, 3_600_000),
    ("web.fetch_max_redirects", 5, "int", "Maximum redirects followed per WebFetch acquisition.", 0, 20),
    ("web.fetch_private_host_allowlist", "", "str", "Comma-separated hostnames, IP addresses, or CIDR blocks exempted from the web fetch plane's private-address block (loopback, RFC1918, and other non-global destinations); link-local and cloud-metadata endpoints are never exempted.", None, None),
    ("collab.min_update_spacing_ms", 20_000, "int", "Minimum milliseconds between assistant collaboration updates.", 1_000, 3_600_000),
    ("collab.tick_ms", 1_500, "int", "Milliseconds between collaboration runtime ticks.", 100, 60_000),
    ("collab.bridge_poll_ms", 750, "int", "Milliseconds between remote-bridge inbox polls.", 50, 60_000),
    ("collab.bridge_poll_deadline_ms", 10_000, "int", "Milliseconds a bridge poll cycle may run before it yields.", 1_000, 600_000),
    ("tasks.monitor_tick_ms", 1_500, "int", "Milliseconds between monitor runtime ticks; per-call tickIntervalMs wins.", 100, 60_000),
    ("tasks.monitor_check_interval_ms", 5_000, "int", "Default milliseconds between monitor checks; per-monitor checkIntervalMs wins.", 250, 3_600_000),
    ("tasks.monitor_heartbeat_interval_ms", 60_000, "int", "Default milliseconds between monitor heartbeats; per-monitor value wins.", 1_000, 86_400_000),
    ("tasks.monitor_narration_spacing_ms", 20_000, "int", "Minimum milliseconds between narrated monitor updates.", 1_000, 3_600_000),
    ("tasks.monitor_output_excerpt_chars", 6_000, "int", "Character cap for narrated monitor output excerpts.", 200, 1_000_000),
    ("tasks.monitor_min_progress_bytes", 120, "int", "Minimum new output bytes for a monitor observation to count as progress.", 1, 1_000_000),
    ("tasks.health_log_max_bytes", 4 * 1024 * 1024, "int", "Byte size at which a health-check log rotates.", 64 * 1024, 1024 * 1024 * 1024),
    ("tasks.health_log_rotations", 3, "int", "Rotated health-check log files kept.", 1, 100),
    ("tasks.health_event_excerpt_bytes", 4_096, "int", "Byte cap per recorded health-check event excerpt.", 256, 10_000_000),
    ("tasks.health_drain_timeout_ms", 10_000, "int", "Milliseconds health-check work gets to drain during shutdown.", 500, 600_000),
    ("tools.task_output_poll_ms", 250, "int", "Milliseconds between TaskOutput polls while blocking on a running task.", 50, 60_000),
    ("subagents.legacy_claim_grace_ms", 30_000, "int", "Milliseconds a legacy continuation claim is honored before recovery may steal it.", 1_000, 3_600_000),
    ("subagents.continuation_lease_ms", 30_000, "int", "Milliseconds a subagent continuation lease stays valid before it may be re-claimed.", 1_000, 3_600_000),
    ("subagents.max_depth", 2, "int", "Maximum nesting depth for subagents spawning subagents.", 1, 10),
    ("subagents.max_recovery_attempts", 3, "int", "Default recovery attempts for an interrupted subagent continuation; policy overrides win.", 0, 20),
    ("agent.max_question_nudges", 3, "int", "Maximum [REPLY REQUIRED] nudges before an unanswered child question is backstopped.", 0, 20),
    ("agent.repeated_tool_cycle_limit", 3, "int", "Identical tool-call cycles tolerated before the progress guard intervenes.", 2, 100),
    ("agent.artifact_acquisition_failure_limit", 3, "int", "Artifact-read failures tolerated before the progress guard intervenes.", 1, 100),
    ("agent.no_progress_limit", 12, "int", "Consecutive no-progress turns tolerated before the turn is stopped.", 2, 1_000),
    ("agent.hook_timeout_ms", 60_000, "int", "Default milliseconds a hook command may run; per-hook timeouts win.", 500, 3_600_000),
    ("mcp.inline_bytes", 8_000, "int", "Inline byte budget for MCP resource payloads before artifact spill.", 500, 10_000_000),
    ("mcp.max_resource_page", 40, "int", "MCP resources listed per page.", 1, 1_000),
    ("mcp.startup_timeout_ms", 10_000, "int", "Default milliseconds for an MCP server to start; per-server settings win.", 500, 600_000),
    ("mcp.request_timeout_ms", 30_000, "int", "Default milliseconds per MCP request; per-server settings win.", 500, 3_600_000),
    ("mcp.connect_timeout_ms", 10_000, "int", "Default milliseconds to establish an MCP HTTP connection.", 500, 600_000),
    ("mcp.headers_timeout_ms", 10_000, "int", "Default milliseconds to receive MCP HTTP response headers.", 500, 600_000),
    ("mcp.body_idle_timeout_ms", 10_000, "int", "Default milliseconds of MCP HTTP body silence tolerated.", 500, 600_000),
    ("mcp.close_grace_ms", 750, "int", "Milliseconds an MCP stdio server gets to exit after close before escalation.", 50, 60_000),
    ("mcp.close_stdin_grace_ms", 100, "int", "Milliseconds after closing an MCP server's stdin before termination proceeds.", 10, 10_000),
    ("mcp.close_kill_wait_ms", 2_000, "int", "Milliseconds to wait for an MCP server to die after SIGKILL.", 100, 60_000),
    ("mcp.blob_keep", 64, "int", "Most-recent MCP blob files kept per blob directory.", 1, 10_000),
    ("process.stdio_leak_grace_ms", 2_000, "int", "Milliseconds to wait for leaked stdio readers after a Bash process exits.", 100, 60_000),
    ("process.bash_termination_grace_ms", 1_000, "int", "Milliseconds a Bash process gets after SIGTERM before SIGKILL.", 100, 60_000),
    ("process.bash_termination_fallback_ms", 4_000, "int", "Milliseconds the Bash termination ladder waits overall before giving up.", 500, 600_000),
    ("process.spool_max_bytes", 64 * 1024 * 1024, "int", "Byte cap for a subprocess output spool file before output is truncated.", 1024 * 1024, 4 * 1024 * 1024 * 1024),
    ("payloads.text_inline_bytes_small", 8000, "int", "Inline byte budget for text tool results in the small payload class; line caps derive from it (~20 bytes/line).", 1_000, 1_000_000),
    ("payloads.read_inline_bytes_small", 8000, "int", "Inline byte budget for Read results in the small payload class.", 1_000, 1_000_000),
    ("payloads.glob_inline_bytes_small", 6000, "int", "Inline byte budget for Glob results in the small payload class.", 1_000, 1_000_000),
    ("payloads.grep_inline_bytes_small", 8000, "int", "Inline byte budget for Grep results in the small payload class.", 1_000, 1_000_000),
    ("payloads.result_final_bytes_small", 12000, "int", "Final serialized byte cap for any tool result in the small payload class.", 2_000, 512_000),
    ("payloads.text_inline_bytes_medium", 12000, "int", "Inline byte budget for text tool results in the medium payload class; line caps derive from it (~20 bytes/line).", 1_000, 1_000_000),
    ("payloads.read_inline_bytes_medium", 12000, "int", "Inline byte budget for Read results in the medium payload class.", 1_000, 1_000_000),
    ("payloads.glob_inline_bytes_medium", 8000, "int", "Inline byte budget for Glob results in the medium payload class.", 1_000, 1_000_000),
    ("payloads.grep_inline_bytes_medium", 12000, "int", "Inline byte budget for Grep results in the medium payload class.", 1_000, 1_000_000),
    ("payloads.result_final_bytes_medium", 18000, "int", "Final serialized byte cap for any tool result in the medium payload class.", 2_000, 512_000),
    ("payloads.text_inline_bytes_large", 18000, "int", "Inline byte budget for text tool results in the large payload class; line caps derive from it (~20 bytes/line).", 1_000, 1_000_000),
    ("payloads.read_inline_bytes_large", 18000, "int", "Inline byte budget for Read results in the large payload class.", 1_000, 1_000_000),
    ("payloads.glob_inline_bytes_large", 10000, "int", "Inline byte budget for Glob results in the large payload class.", 1_000, 1_000_000),
    ("payloads.grep_inline_bytes_large", 18000, "int", "Inline byte budget for Grep results in the large payload class.", 1_000, 1_000_000),
    ("payloads.result_final_bytes_large", 24000, "int", "Final serialized byte cap for any tool result in the large payload class.", 2_000, 512_000),
)


def _declare() -> dict[str, Knob]:
    registry: dict[str, Knob] = {}
    for name, default, kind, description, minimum, maximum in _DECLARATIONS:
        if name in registry:
            raise ValueError(f"Duplicate knob declaration: {name}")
        registry[name] = Knob(name, default, kind, description, minimum, maximum)
    return registry


KNOB_REGISTRY: dict[str, Knob] = _declare()
