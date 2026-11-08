"""Command line entry point: run the stdio MCP server or a lifecycle tool.

`nanobot-teams --config X [--caller Y]` keeps serving the MCP protocol for
existing registrations. New subcommands: `serve` (the same, spelled out),
`bot init`, and `check`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import ConfigError, load_teams_config, resolve_api_keys, resolve_caller
from .ledger import Ledger
from .lifecycle import bot_init, check, print_findings
from .server import serve

_SUBCOMMANDS = ("serve", "bot", "check", "graph", "budget", "audit")


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in _SUBCOMMANDS:
        return _dispatch(args)
    return _run_server(_parse_serve(args))


def _dispatch(args: list[str]) -> int:
    if args[0] == "serve":
        return _run_server(_parse_serve(args[1:]))
    if args[0] == "check":
        return _run_check(_parse_check(args[1:]))
    if args[0] == "graph":
        return _run_graph(_parse_graph(args[1:]))
    if args[0] == "budget":
        return _run_budget(_parse_budget(args[1:]))
    if args[0] == "bot":
        if len(args) >= 2 and args[1] == "init":
            return _run_bot_init(_parse_bot_init(args[2:]))
        print("nanobot-teams bot: expected 'bot init ...'", file=sys.stderr)
        return 2
    return _run_audit(_parse_audit(args[1:]))


def _run_server(args: argparse.Namespace) -> int:
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

    ledger = Ledger(args.ledger) if args.ledger else None
    try:
        asyncio.run(serve(config, api_keys, caller_id, ledger=ledger))
    finally:
        if ledger is not None:
            ledger.close()
    return 0


def _run_check(args: argparse.Namespace) -> int:
    findings = check(Path(args.config), caller=args.caller, probe=not args.no_probe)
    return print_findings(findings)


def _run_bot_init(args: argparse.Namespace) -> int:
    scaffold, findings = bot_init(
        args.bot_id,
        registry_path=Path(args.registry),
        home=Path(args.home),
        port=args.port,
        preset=args.preset,
        display_name=args.display_name,
        role=args.role,
        allowed_callers=args.allow,
    )
    exit_code = print_findings(findings)
    if exit_code == 0:
        for line in scaffold.checklist:
            print(f"- {line}")
    return exit_code


def _run_graph(args: argparse.Namespace) -> int:
    from .config import load_teams_config
    from .reporting import render_graph

    try:
        config = load_teams_config(args.config)
    except ConfigError as exc:
        print(f"nanobot-teams: {exc}", file=sys.stderr)
        return 2
    print(render_graph(config))
    return 0


def _run_budget(args: argparse.Namespace) -> int:
    from .config import load_teams_config
    from .reporting import render_budget

    try:
        config = load_teams_config(args.config)
    except ConfigError as exc:
        print(f"nanobot-teams: {exc}", file=sys.stderr)
        return 2
    print(render_budget(config, [Path(p) for p in args.ledger]))
    return 0


def _run_audit(args: argparse.Namespace) -> int:
    from .reporting import render_audit

    print(
        render_audit(
            [Path(p) for p in args.ledger],
            limit=args.limit,
            target=args.target,
            status=args.status,
        )
    )
    return 0


def _parse_graph(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="nanobot-teams graph")
    parser.add_argument("--config", required=True, help="path to teams.json")
    return parser.parse_args(argv)


def _parse_budget(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="nanobot-teams budget")
    parser.add_argument("--config", required=True, help="path to teams.json")
    parser.add_argument(
        "--ledger",
        action="append",
        default=[],
        help="ledger file to read; repeatable; defaults to the registry directory",
    )
    return parser.parse_args(argv)


def _parse_audit(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="nanobot-teams audit")
    parser.add_argument(
        "--ledger",
        action="append",
        required=True,
        help="ledger file to read; repeatable",
    )
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--target")
    parser.add_argument("--status")
    return parser.parse_args(argv)


def _parse_serve(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="nanobot-teams serve")
    parser.add_argument("--config", required=True, help="path to teams.json")
    parser.add_argument(
        "--caller",
        help="bot id this MCP instance acts for; required for schema 2 registries",
    )
    parser.add_argument(
        "--ledger",
        help="path to the SQLite ledger; without it no delegation is recorded",
    )
    parser.add_argument("--verbose", action="store_true", help="log debug detail")
    return parser.parse_args(argv)


def _parse_check(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="nanobot-teams check")
    parser.add_argument("--config", required=True, help="path to teams.json")
    parser.add_argument("--caller", help="limit the check to one caller's view")
    parser.add_argument(
        "--no-probe", action="store_true", help="skip the endpoint socket probes"
    )
    return parser.parse_args(argv)


def _parse_bot_init(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="nanobot-teams bot init")
    parser.add_argument("bot_id", help="id of the bot to create")
    parser.add_argument("--registry", required=True, help="path to the schema 2 teams.json")
    parser.add_argument("--home", required=True, help="home directory to create for the bot")
    parser.add_argument("--port", required=True, type=int, help="API port for the bot")
    parser.add_argument("--preset", required=True, help="model preset name")
    parser.add_argument("--display-name", help="display name; defaults to the id")
    parser.add_argument("--role", default="", help="one-line role description")
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        dest="allow",
        help="bot id allowed to call the new bot; repeatable",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
