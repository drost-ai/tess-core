"""The agent turn engine: loop, provider turns, tool dispatch, checkpoints.

The facade re-exports exactly the public names its submodules define.
"""
from __future__ import annotations

from .checkpoints import (
    maybe_auto_checkpoint_raw_session,
    maybe_auto_compact_session,
)
from .context import (
    emit_continuation_relief,
    is_context_window_failure,
    prepare_provider_request,
    record_continuation_relief,
)
from .context_budget import (
    apply_provider_token_budget,
    assess_context_headroom,
    enforce_context_headroom,
    headroom_event_payload,
)
from .dynamic_context import (
    dynamic_context_belongs_in_messages,
    inject_system_context_into_messages,
)
from .continuity import readOpenChildQuestions, run_parent_continuity_turn
from .loop import run_agent_prompt
from .progress import (
    LogicalTurnProgressLedger,
    MAX_RECORDED_ARTIFACTS,
    MAX_RECORDED_FINGERPRINTS,
    MAX_REPEATED_CYCLE_BATCHES,
    PROGRESS_LEDGER_SCHEMA_VERSION,
    append_tool_result_batch,
    no_progress_event_data,
    no_progress_turn_result,
)
from .prompt_content import (
    extract_assistant_text,
    normalize_agent_prompt,
)
from .provider_turn import (
    add_empty_response_continuation_nudge,
    complete_empty_provider_response,
    is_empty_assistant_response,
    record_empty_response_usage,
    request_provider_response,
)
from .request_integrity import (
    STRICT_TOOL_PROTOCOL_PROVIDERS,
    normalize_provider_request_messages,
)
from .shared import (
    IMAGE_INPUT_UNSUPPORTED_MESSAGE,
    model_supports_image_inputs,
    request_has_image_inputs,
    unsupported_image_input_response,
)
from .stable_boundary import (
    finish_stable_boundary,
    run_stable_boundary_hook,
)
from .tool_context import build_tool_context
from .tool_dispatch import (
    SerialQueue,
    ToolDispatchOutcome,
    approval_skip_result,
    create_serial_queue,
    invalid_tool_input_message,
    resolve_tool_concurrency,
    run_tool_dispatch_batch,
)
from .tool_hooks import (
    build_tool_hook_context,
    run_post_tool_hook,
    run_pre_tool_hook,
)
from .tool_result_budget import (
    ToolResultBatchBudget,
)
from .turn_session import (
    resolve_effective_provider_turn_binding,
    run_in_provider_turn_session,
    run_with_transient_execution_session,
)

__all__ = [
    "IMAGE_INPUT_UNSUPPORTED_MESSAGE",
    "LogicalTurnProgressLedger",
    "MAX_RECORDED_ARTIFACTS",
    "MAX_RECORDED_FINGERPRINTS",
    "MAX_REPEATED_CYCLE_BATCHES",
    "PROGRESS_LEDGER_SCHEMA_VERSION",
    "STRICT_TOOL_PROTOCOL_PROVIDERS",
    "SerialQueue",
    "ToolDispatchOutcome",
    "ToolResultBatchBudget",
    "add_empty_response_continuation_nudge",
    "append_tool_result_batch",
    "apply_provider_token_budget",
    "approval_skip_result",
    "assess_context_headroom",
    "build_tool_context",
    "build_tool_hook_context",
    "complete_empty_provider_response",
    "create_serial_queue",
    "dynamic_context_belongs_in_messages",
    "emit_continuation_relief",
    "enforce_context_headroom",
    "extract_assistant_text",
    "finish_stable_boundary",
    "headroom_event_payload",
    "inject_system_context_into_messages",
    "invalid_tool_input_message",
    "is_context_window_failure",
    "is_empty_assistant_response",
    "maybe_auto_checkpoint_raw_session",
    "maybe_auto_compact_session",
    "model_supports_image_inputs",
    "no_progress_event_data",
    "no_progress_turn_result",
    "normalize_provider_request_messages",
    "normalize_agent_prompt",
    "prepare_provider_request",
    "record_continuation_relief",
    "record_empty_response_usage",
    "request_has_image_inputs",
    "request_provider_response",
    "resolve_effective_provider_turn_binding",
    "resolve_tool_concurrency",
    "run_agent_prompt",
    "run_parent_continuity_turn",
    "readOpenChildQuestions",
    "run_in_provider_turn_session",
    "run_post_tool_hook",
    "run_pre_tool_hook",
    "run_stable_boundary_hook",
    "run_tool_dispatch_batch",
    "run_with_transient_execution_session",
    "unsupported_image_input_response",
]
