"""Test-support surface: the internals integration tests exercise directly.

Aggregated here so tests have one import point for cross-subsystem seams
(compaction planning, provider wire builders, and the *ForTests hooks).
"""
from __future__ import annotations

from .authority.executable_closure import (
    __setExecutableClosureTestHookForTests,
    inspectExecutableClosure,
    snapshotExecutableClosure,
)
from .remote.bridge_recovery import __setBridgeRecoveryTestHookForTests
from .context.compaction import (
    build_compaction_system_prompt_context,
    build_messages_for_working_context,
    estimate_working_context_tokens,
    run_compaction,
    should_auto_compact,
)
from .context.compaction_transport import (
    CompactionModelClient,
    create_default_compaction_model_client,
    resolve_compaction_execution_plan,
    resolve_compaction_model_candidates,
    resolve_compaction_token_policy,
)
from .context.continuation import apply_context_relief_modes, apply_lightweight_context_relief, classify_context_pressure
from .context.token_accounting import measure_request_input_tokens
from .providers.anthropic import (
    build_anthropic_count_tokens_payload,
    build_anthropic_headers,
    build_anthropic_messages_payload,
    normalize_anthropic_sse_response,
    resolve_anthropic_count_tokens_url,
    resolve_anthropic_messages_url,
    send_anthropic_messages_request,
)
from .providers.errors import create_provider_failure_record, create_provider_request_error, is_provider_request_error
from .providers.openai import (
    DEFAULT_CODEX_BASE_URL,
    build_chat_completions_create_params,
    build_responses_create_params,
    send_openai_chat_completions_request,
    send_openai_responses_request,
)
from .runtime.secrets import resolve_runtime_secret_string
