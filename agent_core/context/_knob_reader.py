from __future__ import annotations

"""Shared integer knob reader for the context family.

The import of the knob machinery happens at call time because the config
package imports context modules during load.
"""


def context_knob(name: str) -> int:
    from ..config._knobs import knobValue

    return int(knobValue(name))
