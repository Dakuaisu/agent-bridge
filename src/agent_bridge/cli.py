"""agent-bridge command line."""

from __future__ import annotations

import argparse

from agent_bridge import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-bridge", description=__doc__)
    parser.add_argument("--version", action="version", version=f"agent-bridge {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
