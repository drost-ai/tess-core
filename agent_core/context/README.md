# Context Management Package

This package owns context-window and memory behavior:

- `compaction/`: checkpoint-v2 compaction flow, cutoffs, evidence, synthesis,
  verification, artifacts, and the auto-compaction policy/projection.
- `compaction_models.py`: provider/model selection and timeout handling for
  compaction calls.
- `continuation/`: context relief mode selection, reinjection, and reactive
  retry helpers.
- `durable_memory.py`: project memory extraction, retrieval, and manual sync.
- `token_accounting.py`: preflight and actual token accounting helpers.

Import from `agent_core.context.*`; the legacy flat module paths were retired
in the v1 refactor.
