from __future__ import annotations

import sys

from .headless import headless_main


if __name__ == "__main__":
    raise SystemExit(headless_main(sys.argv[1:]))
