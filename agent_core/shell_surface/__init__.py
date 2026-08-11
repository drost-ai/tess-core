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
    buildAgentsReport,
    formatChildAgentRecord,
)
from .common import (
    squashWhitespace,
    formatMonitorRecord,
    truncateAscii,
)
from .context_commands import (
    buildCompactionStatusReport,
    buildMonitorsReport,
)
from .debug_bundle import createDebugBundle
from .diagnostics import (
    buildCollaborationStatusReport,
    buildReviewPrompt,
    formatDebugBundleReport,
    formatDiagnosticsReport,
    format_session_candidate,
    formatTranscriptEntries,
)
from .dispatcher import handleSlashCommand
from .registry import (
    buildSlashHelp,
    getSlashCommandAvailability,
    listSlashCommands,
)
from .remote_background import (
    buildBackgroundReport,
    buildDaemonReport,
    buildRemoteFallbackReport,
    buildRemoteListReport,
    buildRemoteReport,
    formatRemoteSessionRecord,
)
from .remote_select import (
    canPromptForSelection,
    promptToPickRemoteSession,
    resolveRemoteSelection,
    resolveTeleportSession,
)
from .shared import (
    AVAILABLE_TUI_THEMES,
    PERMISSION_MODES,
    PERMISSION_MODE_ORDER,
)
from .status import (
    buildPermissionsReport,
    buildProviderProfilesReport,
    buildStatusReport,
    buildTodosReport,
    compactModelName,
    formatAutoModePolicy,
    formatPreflightTokenSummary,
    formatUsageBreakdown,
    formatUsageSummary,
    normalizeModel,
)
from .tasks import (
    buildTasksReport,
    formatTaskInspectReport,
    formatTaskListReport,
    formatTaskResultReport,
    formatTaskSurface,
)

__all__ = [
    "AVAILABLE_TUI_THEMES",
    "PERMISSION_MODES",
    "PERMISSION_MODE_ORDER",
    "buildAgentsReport",
    "buildBackgroundReport",
    "buildCollaborationStatusReport",
    "buildCompactionStatusReport",
    "buildDaemonReport",
    "buildMonitorsReport",
    "buildPermissionsReport",
    "buildProviderProfilesReport",
    "buildRemoteFallbackReport",
    "buildRemoteListReport",
    "buildRemoteReport",
    "buildReviewPrompt",
    "buildSlashHelp",
    "buildStatusReport",
    "buildTasksReport",
    "buildTodosReport",
    "squashWhitespace",
    "compactModelName",
    "createDebugBundle",
    "formatAutoModePolicy",
    "formatChildAgentRecord",
    "formatDebugBundleReport",
    "formatDiagnosticsReport",
    "formatMonitorRecord",
    "formatPreflightTokenSummary",
    "formatRemoteSessionRecord",
    "format_session_candidate",
    "formatTaskInspectReport",
    "formatTaskListReport",
    "formatTaskResultReport",
    "formatTaskSurface",
    "formatTranscriptEntries",
    "formatUsageBreakdown",
    "formatUsageSummary",
    "getSlashCommandAvailability",
    "handleSlashCommand",
    "listSlashCommands",
    "normalizeModel",
    "truncateAscii",
]
