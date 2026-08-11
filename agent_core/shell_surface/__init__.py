"""Slash-command surface: registry, dispatcher, and command handlers.

The facade re-exports the public names its submodules define, plus the
shell-entrypoint names of docs/public-api-contract.md whose implementations
live in other planes (transcript entry builders, image budget estimation).
"""
from __future__ import annotations

from ..contracts.providers import AnthropicBlock
from ..payloads.image_budget import estimate_encoded_image_request_bytes
from ..session.transcript import (
    build_transcript_snapshot,
    make_text_entry,
    make_tool_entry,
    merge_transcript_entry,
    normalize_transcript_body,
)

from .agents import (
    build_agents_report,
    format_child_agent_record,
)
from .common import (
    squash_whitespace,
    format_monitor_record,
    truncate_ascii,
)
from .context_commands import (
    build_compaction_status_report,
    build_monitors_report,
)
from .debug_bundle import create_debug_bundle
from .diagnostics import (
    build_collaboration_status_report,
    build_review_prompt,
    format_debug_bundle_report,
    format_diagnostics_report,
    format_session_candidate,
    format_transcript_entries,
)
from .dispatcher import handle_slash_command
from .registry import (
    build_slash_help,
    get_slash_command_availability,
    list_slash_commands,
)
from .remote_background import (
    build_background_report,
    build_daemon_report,
    build_remote_fallback_report,
    build_remote_list_report,
    build_remote_report,
    format_remote_session_record,
)
from .remote_select import (
    can_prompt_for_selection,
    prompt_to_pick_remote_session,
    resolve_remote_selection,
    resolve_teleport_session,
)
from .shared import (
    AVAILABLE_TUI_THEMES,
    PERMISSION_MODES,
    PERMISSION_MODE_ORDER,
)
from .status import (
    build_permissions_report,
    build_provider_profiles_report,
    build_status_report,
    build_todos_report,
    compact_model_name,
    format_auto_mode_policy,
    format_preflight_token_summary,
    format_usage_breakdown,
    format_usage_summary,
    normalize_model,
)
from .tasks import (
    build_tasks_report,
    format_task_inspect_report,
    format_task_list_report,
    format_task_result_report,
    format_task_surface,
)

__all__ = [
    "AVAILABLE_TUI_THEMES",
    "PERMISSION_MODES",
    "PERMISSION_MODE_ORDER",
    "build_agents_report",
    "build_background_report",
    "build_collaboration_status_report",
    "build_compaction_status_report",
    "build_daemon_report",
    "build_monitors_report",
    "build_permissions_report",
    "build_provider_profiles_report",
    "build_remote_fallback_report",
    "build_remote_list_report",
    "build_remote_report",
    "build_review_prompt",
    "build_slash_help",
    "build_status_report",
    "build_tasks_report",
    "build_todos_report",
    "squash_whitespace",
    "compact_model_name",
    "create_debug_bundle",
    "format_auto_mode_policy",
    "format_child_agent_record",
    "format_debug_bundle_report",
    "format_diagnostics_report",
    "format_monitor_record",
    "format_preflight_token_summary",
    "format_remote_session_record",
    "format_session_candidate",
    "format_task_inspect_report",
    "format_task_list_report",
    "format_task_result_report",
    "format_task_surface",
    "format_transcript_entries",
    "format_usage_breakdown",
    "format_usage_summary",
    "get_slash_command_availability",
    "handle_slash_command",
    "list_slash_commands",
    "normalize_model",
    "truncate_ascii",
]
