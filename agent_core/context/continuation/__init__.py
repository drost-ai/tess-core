"""Continuation context and staged relief.

The facade re-exports exactly the public names its submodules define.
"""
from __future__ import annotations

from .compaction import (
    apply_compact_prefix,
    apply_partial_compaction,
    choose_preserved_raw_start,
    compact_historical_tail,
    compact_message_for_continuation,
    compact_tool_input,
    find_last_plain_user_index,
    summarize_tool_result_for_relief,
)
from .context import build_continuation_context
from .parsing import (
    empty_compaction_result,
    empty_relief_summary,
    extract_command,
    extract_file_path,
    omit_none,
    parse_structured_tool_result,
)
from .relief import (
    apply_context_relief_mode,
    apply_context_relief_modes,
    apply_lightweight_context_relief,
    choose_context_relief_modes,
    classify_context_relief_pressure,
    is_context_pressure_relieved,
    supports_api_microcompact,
)
from .rendering import (
    append_summary_section,
    normalize_reinjection_lines,
    render_context_relief_prompt,
    render_continuation_overlay,
    render_partial_compaction_summary,
    render_region_summary_reinjection_lines,
    render_todo_overlay,
    summarize_continuation_region,
)
from .shared import (
    LOW_VALUE_TOOL_NAMES,
)
from .tokens import (
    as_record,
    classify_context_pressure,
    collapse_whitespace,
    estimate_messages_tokens,
    estimateTextTokens,
    estimate_todo_and_plan_tokens,
    truncate,
)

__all__ = [
    "LOW_VALUE_TOOL_NAMES",
    "append_summary_section",
    "apply_compact_prefix",
    "apply_context_relief_mode",
    "apply_context_relief_modes",
    "apply_lightweight_context_relief",
    "apply_partial_compaction",
    "as_record",
    "build_continuation_context",
    "choose_context_relief_modes",
    "choose_preserved_raw_start",
    "classify_context_pressure",
    "classify_context_relief_pressure",
    "collapse_whitespace",
    "compact_historical_tail",
    "compact_message_for_continuation",
    "compact_tool_input",
    "empty_compaction_result",
    "empty_relief_summary",
    "estimate_messages_tokens",
    "estimateTextTokens",
    "estimate_todo_and_plan_tokens",
    "extract_command",
    "extract_file_path",
    "find_last_plain_user_index",
    "is_context_pressure_relieved",
    "normalize_reinjection_lines",
    "omit_none",
    "parse_structured_tool_result",
    "render_context_relief_prompt",
    "render_continuation_overlay",
    "render_partial_compaction_summary",
    "render_region_summary_reinjection_lines",
    "render_todo_overlay",
    "summarize_continuation_region",
    "summarize_tool_result_for_relief",
    "supports_api_microcompact",
    "truncate",
]
