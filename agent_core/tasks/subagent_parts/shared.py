from __future__ import annotations

import os
import subprocess
import sys
import uuid
from typing import Any

from ..._utils import maybe_await, now_iso, pretty_json
from ...config import buildSystemPromptForAgent, normalizeModelName, resolveModelForAgent, resolveProviderForModelSelection
from ...storage import createEmptySession, loadChildAgent, mutateChildAgent, mutateTaskMeta, saveChildAgent, saveSession, taskOutputPath, writeTaskMeta
from ...subagent_contract import runtime_contract_for_agent, summarize_subagent_runtime_contract
from ...subagent_result import evaluate_subagent_result, persist_structured_subagent_result
from ...task_communication import append_task_event
from ...workspace import createGitWorktree


PERMISSION_MODES = {"default", "acceptEdits", "bypassPermissions", "dontAsk", "auto", "plan"}
