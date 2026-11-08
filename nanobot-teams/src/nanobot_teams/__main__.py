"""Command line entry point: run the stdio MCP server with a registry file."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from .config import ConfigError, load_teams_config, resolve_api_keys, resolve_caller
from .server import serve


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )
    try:
        config = load_teams_config(args.config)
        caller_id = resolve_caller(config, args.caller)
        api_keys = resolve_api_keys(config, caller_id)
    except ConfigError as exc:
        print(f"nanobot-teams: {exc}", file=sys.stderr)
        return 2

    asyncio.run(serve(config, api_keys, caller_id))
    return 0


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="nanobot-teams")
    parser.add_argument("--config", required=True, help="path to teams.json")
    parser.add_argument(
        "--caller",
        help="bot id this MCP instance acts for; required for schema 2 registries",
    )
    parser.add_argument("--verbose", action="store_true", help="log debug detail")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
