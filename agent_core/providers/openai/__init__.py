"""OpenAI adapter: chat completions and Responses, streaming, recovery.

The facade re-exports exactly the public names its submodules define.
"""
from __future__ import annotations

from .chat import sendOpenAiChatCompletionsRequest
from .params import (
    buildChatCompletionsCreateParams,
    buildResponsesCreateParams,
    convertMessagesToChatCompletions,
    convertMessagesToInput,
    getOpenAiProviderLabel,
    isCodexBaseUrl,
    resolveOpenAiBaseUrl,
    supportsOpenAiChatCompletionsReasoningEffort,
)
from .reconcile import (
    OpenAiEmptyCompletedResponse,
    OpenAiReconciliationConflict,
    TERMINAL_RECONCILIATION_SCHEMA_VERSION,
    close_openai_output_item,
    function_call_projection_record,
    message_projection_record,
    normalize_terminal_projection,
    projection_blocks,
    reconciliation_conflict_failure,
    resolve_terminal_projection,
    select_authoritative_projection,
)
from .recovery import (
    append_openai_partial,
    attach_interrupted_preview,
    attach_openai_failure_lineage,
    attach_recovery_failure,
    authoritative_openai_response,
    can_retry_openai_generation,
    commit_openai_delivery,
    create_openai_logical_call_id,
    finalize_interrupted_openai_preview,
    partial_openai_event_lineage,
    sink_failure,
    supersede_openai_generation,
)
from .recovery_attempt import (
    openai_retry_delay_ms,
    recover_openai_attempt,
)
from .responses import (
    sendOpenAiRequest,
    sendOpenAiResponsesRequest,
)
from .responses_failures import response_terminal_failure
from .responses_session import (
    extract_turn_state,
    finalize_responses_success,
    projection_event_fields,
    responses_activity_kind,
    start_responses_stream,
    stateless_prepared_request,
    websocket_response_create_params,
)
from .responses_terminal import handle_response_completed
from .shared import (
    OPENAI_RETRY_DELAYS_MS,
    DEFAULT_CODEX_BASE_URL,
    DEFAULT_CODEX_INSTRUCTIONS,
    OPENAI_ABORT_POLL_INTERVAL_MS,
    OPENAI_REQUEST_START_TIMEOUT_MS,
    OPENAI_STREAM_IDLE_TIMEOUT_MS,
)

__all__ = [
    "OPENAI_RETRY_DELAYS_MS",
    "DEFAULT_CODEX_BASE_URL",
    "DEFAULT_CODEX_INSTRUCTIONS",
    "OPENAI_ABORT_POLL_INTERVAL_MS",
    "OPENAI_REQUEST_START_TIMEOUT_MS",
    "OPENAI_STREAM_IDLE_TIMEOUT_MS",
    "OpenAiEmptyCompletedResponse",
    "OpenAiReconciliationConflict",
    "TERMINAL_RECONCILIATION_SCHEMA_VERSION",
    "append_openai_partial",
    "attach_interrupted_preview",
    "attach_openai_failure_lineage",
    "attach_recovery_failure",
    "authoritative_openai_response",
    "buildChatCompletionsCreateParams",
    "buildResponsesCreateParams",
    "can_retry_openai_generation",
    "close_openai_output_item",
    "commit_openai_delivery",
    "convertMessagesToChatCompletions",
    "convertMessagesToInput",
    "create_openai_logical_call_id",
    "extract_turn_state",
    "finalize_interrupted_openai_preview",
    "finalize_responses_success",
    "function_call_projection_record",
    "getOpenAiProviderLabel",
    "handle_response_completed",
    "isCodexBaseUrl",
    "message_projection_record",
    "normalize_terminal_projection",
    "openai_retry_delay_ms",
    "partial_openai_event_lineage",
    "projection_blocks",
    "projection_event_fields",
    "reconciliation_conflict_failure",
    "recover_openai_attempt",
    "resolveOpenAiBaseUrl",
    "resolve_terminal_projection",
    "response_terminal_failure",
    "responses_activity_kind",
    "select_authoritative_projection",
    "sendOpenAiChatCompletionsRequest",
    "sendOpenAiRequest",
    "sendOpenAiResponsesRequest",
    "sink_failure",
    "start_responses_stream",
    "stateless_prepared_request",
    "supersede_openai_generation",
    "supportsOpenAiChatCompletionsReasoningEffort",
    "websocket_response_create_params",
]
