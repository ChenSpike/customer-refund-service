"""Retired entry point for the pre-integration Policy-only benchmark.

The final branch has one supported live workflow: the fixed ``demo01`` through
``demo20`` application entered through ``refund_app.api`` (or ``main.py`` for a
graph-level diagnostic).  Keeping the former ``main_db`` mutation commands
available would make it too easy to run a Policy-only benchmark against the
wrong database and mislabel it as full-system evidence.
"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description=(
            "The standalone Policy-only CLI is retired on branch final. "
            "Use the refund HTTP API for full-stack execution, main.py for a "
            "graph-level diagnostic, and db.admin for guarded final-database operations."
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
