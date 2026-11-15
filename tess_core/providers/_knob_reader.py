from __future__ import annotations

"""Shared knob reader for the provider family.

The import of the knob machinery happens at call time because the config
package imports provider modules during load.
"""

from typing import Any


def provider_knob(name: str) -> Any:
    from ..config._knobs import knob_value

    return knob_value(name)
