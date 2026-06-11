from __future__ import annotations

import os
import subprocess
import sys
import uuid
from typing import Any

from ..._utils import maybe_await, now_iso, pretty_json
from ...config import buildSystemPromptForAgent, normalizeModelName, resolveModelForAgent, resolveProviderForModelSelection
from ...storage import createEmptySession, loadChildAgent, mutateChildAgent, mutateTaskMeta, saveChildAgent, saveSession, taskOutputPath, writeTaskMeta
from ...workspace import createGitWorktree


PERMISSION_MODES = {"default", "acceptEdits", "bypassPermissions", "dontAsk", "auto", "plan"}
