"""Subagent policy and durable state: depth, capacity, contracts, launch groups, reconciliation.

The facade re-exports exactly the public names its submodules define.
"""
from __future__ import annotations

from .capacity import ACTIVE_TASK_STATUSES, check_subagent_capacity, resolve_subagent_concurrency_policy
from .contract import (
    filter_tools_for_subagent,
    normalize_subagent_runtime_contract,
    runtime_contract_for_agent,
    summarize_subagent_runtime_contract,
)
from .coordination import (
    claim_unconsumed_subagent_reconciliations,
    complete_subagent_reconciliation_claim,
    list_subagent_launch_groups,
    list_subagent_reconciliations,
    upsert_subagent_launch_group,
    with_subagent_coordination_transaction,
)
from .depth_policy import (
    check_subagent_depth_allowed,
    require_subagent_depth_allowed,
    resolve_subagent_max_depth,
)
from .groups import append_launch_group_metadata, resolve_subagent_launch_group_id
from .limits import build_subagent_tool_limit_result, check_subagent_runtime_limits, resolve_subagent_runtime_limits
from .reconciliation import (
    TERMINAL_RECONCILIATION_STATUSES,
    append_pending_subagent_reconciliations_to_session,
    build_subagent_reconciliation_record,
    consume_appended_subagent_reconciliations,
    create_subagent_reconciliation_for_terminal_child,
    list_unconsumed_subagent_reconciliations,
    mark_subagent_reconciliations_consumed,
    read_subagent_reconciliations,
    render_subagent_reconciliation_message,
    subagent_reconciliation_log_path,
    upsert_subagent_reconciliation,
)
from .result import (
    evaluate_subagent_result,
    extract_structured_result_json,
    persist_structured_subagent_result,
    structured_result_path,
)
from .finalizers import (
    finalize_background_subagent_failure_from_env,
    finalize_background_subagent_success_from_env,
)
from .lifecycle import prepare_subagent, start_background_subagent
from .permissions import (
    inherit_parent_permission_mode,
    map_stop_reason_to_completion_reason,
    map_stop_reason_to_status,
    map_stop_reason_to_waiting_reason,
    sanitize_permission_mode,
)

__all__ = [
    "ACTIVE_TASK_STATUSES",
    "TERMINAL_RECONCILIATION_STATUSES",
    "append_launch_group_metadata",
    "append_pending_subagent_reconciliations_to_session",
    "build_subagent_reconciliation_record",
    "build_subagent_tool_limit_result",
    "check_subagent_depth_allowed",
    "check_subagent_capacity",
    "check_subagent_runtime_limits",
    "claim_unconsumed_subagent_reconciliations",
    "complete_subagent_reconciliation_claim",
    "consume_appended_subagent_reconciliations",
    "create_subagent_reconciliation_for_terminal_child",
    "evaluate_subagent_result",
    "extract_structured_result_json",
    "filter_tools_for_subagent",
    "finalize_background_subagent_failure_from_env",
    "finalize_background_subagent_success_from_env",
    "inherit_parent_permission_mode",
    "list_subagent_launch_groups",
    "list_subagent_reconciliations",
    "list_unconsumed_subagent_reconciliations",
    "map_stop_reason_to_completion_reason",
    "map_stop_reason_to_status",
    "map_stop_reason_to_waiting_reason",
    "mark_subagent_reconciliations_consumed",
    "normalize_subagent_runtime_contract",
    "persist_structured_subagent_result",
    "prepare_subagent",
    "read_subagent_reconciliations",
    "render_subagent_reconciliation_message",
    "require_subagent_depth_allowed",
    "resolve_subagent_launch_group_id",
    "resolve_subagent_max_depth",
    "resolve_subagent_concurrency_policy",
    "resolve_subagent_runtime_limits",
    "runtime_contract_for_agent",
    "sanitize_permission_mode",
    "start_background_subagent",
    "structured_result_path",
    "subagent_reconciliation_log_path",
    "summarize_subagent_runtime_contract",
    "upsert_subagent_launch_group",
    "upsert_subagent_reconciliation",
    "with_subagent_coordination_transaction",
]
