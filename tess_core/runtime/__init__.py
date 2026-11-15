"""Runtime subsystem: bootstrap, events, state, paths, identity, and secrets.

This facade is deliberately empty. ``runtime.bootstrap`` pulls in most of the
kernel, so importing it here would mean that importing any *leaf* of this
package -- ``runtime.identifiers`` and ``runtime.paths`` are both needed early
by storage -- would drag the whole graph in and cycle back through storage.

Import the submodule you need directly (``from tess_core.runtime.paths import
...``). ``tess_core`` re-exports the bootstrap surface, so names previously
reached through ``tess_core.runtime`` remain available on the top-level
package.
"""
