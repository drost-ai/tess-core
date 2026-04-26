# Context Management Package

This package owns context-window and memory behavior:

- `compaction.py`: checkpoint-v2 compaction flow and working-context shaping.
- `compaction_models.py`: provider/model selection and timeout handling for
  compaction calls.
- `continuation.py`: context relief mode selection, reinjection, and reactive
  retry helpers.
- `durable_memory.py`: project memory extraction, retrieval, and manual sync.
- `token_accounting.py`: preflight and actual token accounting helpers.

Top-level modules such as `agent_core.compaction` and
`agent_core.continuation` remain compatibility shims. New code should import
from `agent_core.context.*` when it is not relying on the legacy public module
path.
