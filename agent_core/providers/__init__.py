"""Provider adapters and the shared provider-boundary services.

This facade is deliberately empty. Star-importing the adapters here would mean
that importing a leaf of this package -- ``providers.errors`` is needed by
config and the agent loop -- would drag both transport stacks in and cycle back
through storage. Import the submodule you need directly; ``agent_core``
re-exports the adapter surface.
"""
