from __future__ import annotations

import argparse
import json
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="agent-core Landlock launcher shim")
    parser.add_argument("--policy", required=True)
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    _ = json.loads(args.policy)
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not argv:
        raise SystemExit("missing command after --")
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main()
