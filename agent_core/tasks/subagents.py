from __future__ import annotations

import sys as _sys
import types as _types

from .subagent_parts import *
from .subagent_parts import _modules as _SUBAGENT_PART_MODULES


_PUBLIC_EXPORTS = ['Any', 'PERMISSION_MODES', 'annotations', 'buildSystemPromptForAgent', 'createEmptySession', 'createGitWorktree', 'finalizeBackgroundSubagentFailureFromEnv', 'finalizeBackgroundSubagentSuccessFromEnv', 'inheritParentPermissionMode', 'loadChildAgent', 'mapStopReasonToCompletionReason', 'mapStopReasonToStatus', 'mapStopReasonToWaitingReason', 'maybe_await', 'normalizeModelName', 'now_iso', 'os', 'prepareSubagent', 'pretty_json', 'resolveModelForAgent', 'resolveProviderForModelSelection', 'runForegroundSubagent', 'sanitizePermissionMode', 'saveChildAgent', 'saveSession', 'startBackgroundSubagent', 'subprocess', 'sys', 'taskOutputPath', 'uuid', 'writeTaskMeta']

__all__ = _PUBLIC_EXPORTS


def __dir__():
    return sorted(_PUBLIC_EXPORTS)


class _SubagentsModule(_types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        for module in _SUBAGENT_PART_MODULES:
            if name in vars(module):
                setattr(module, name, value)


_sys.modules[__name__].__class__ = _SubagentsModule
