"""Command line entry point: run the stdio MCP server or a lifecycle tool.

`nanobot-teams --config X [--caller Y]` keeps serving the MCP protocol for
existing registrations. New subcommands: `serve` (the same, spelled out),
`bot init`, and `check`.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import ConfigError, load_teams_config, resolve_api_keys, resolve_caller
from .ledger import Ledger, LedgerError
from .lifecycle import bot_init, check, print_findings
from .server import serve

_SUBCOMMANDS = (
    "serve", "bot", "check", "graph", "budget", "audit", "ack-unknown",
    "coordinator", "async-mcp",
)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in _SUBCOMMANDS:
        return _dispatch(args)
    return _run_server(_parse_serve(args))


def _dispatch(args: list[str]) -> int:
    if args[0] == "coordinator":
        return _run_coordinator(_parse_coordinator(args[1:]))
    if args[0] == "async-mcp":
        return _run_async_mcp(_parse_async_mcp(args[1:]))
    if args[0] == "ack-unknown":
        return _run_ack(args[1:])
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

    ledger = None
    try:
        ledger_path = args.ledger or Path(args.config).resolve().parent / ".teams-state" / f"{caller_id}.sqlite3"
        ledger = Ledger(ledger_path)
        ledger.bind(config.team_id, caller_id)
        asyncio.run(serve(config, api_keys, caller_id, ledger=ledger))
    except LedgerError as exc:
        print(f"nanobot-teams: {exc}", file=sys.stderr)
        return 2
    finally:
        if ledger is not None:
            ledger.close()
    return 0


def _run_check(args: argparse.Namespace) -> int:
    findings = check(Path(args.config), caller=args.caller, probe=not args.no_probe,
                     deployment=Path(args.deployment) if args.deployment else None)
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
        capabilities=args.capability,
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
    paths = [Path(p) for p in args.ledger] or sorted((Path(args.config).resolve().parent / ".teams-state").glob("*.sqlite3"))
    report = render_budget(config, paths)
    print(report)
    if report.startswith(("unavailable:", "missing:")):
        return 2
    return 0


def _run_audit(args: argparse.Namespace) -> int:
    from .reporting import render_audit

    report = render_audit(
            [Path(p) for p in args.ledger],
            limit=args.limit,
            target=args.target,
            status=args.status,
    )
    print(report)
    return 2 if report.startswith(("unavailable:", "missing:")) else 0


def _parse_coordinator(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="nanobot-teams coordinator")
    parser.add_argument("--config", required=True, help="path to coordinator.json")
    return parser.parse_args(argv)


def _parse_async_mcp(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="nanobot-teams async-mcp")
    parser.add_argument("--url", required=True, help="coordinator base URL")
    parser.add_argument("--token-env", required=True, help="env var carrying the planner token")
    return parser.parse_args(argv)


def _run_coordinator(args: argparse.Namespace) -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )
    try:
        from .coordinator.config import ConfigError
        from .coordinator.http_api import build_app
        from .coordinator.service import CoordinatorService

        service = CoordinatorService.from_config_path(args.config)
    except (ConfigError, Exception) as exc:
        if type(exc) is Exception:  # unexpected import/runtime issue
            raise
        print(f"nanobot-teams coordinator: {exc}", file=sys.stderr)
        return 2
    try:
        import asyncio

        return asyncio.run(service.run_forever(build_app))
    finally:
        service.close()


def _run_async_mcp(args: argparse.Namespace) -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )
    import asyncio

    from .async_mcp import serve_async

    try:
        asyncio.run(serve_async(args.url, args.token_env))
    except KeyboardInterrupt:
        return 0
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
        help="ledger file to read; repeatable; defaults to <registry-dir>/.teams-state/*.sqlite3",
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
        help="persistent SQLite control state; defaults to <registry-dir>/.teams-state/<caller>.sqlite3",
    )
    parser.add_argument("--verbose", action="store_true", help="log debug detail")
    return parser.parse_args(argv)


def _parse_check(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="nanobot-teams check")
    parser.add_argument("--config", required=True, help="path to teams.json")
    parser.add_argument("--caller", help="limit the check to one caller's view")
    parser.add_argument("--deployment", help="operator-declared timeout contract JSON")
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
    parser.add_argument("--role", required=True, help="one-line role description")
    parser.add_argument(
        "--capability",
        action="append",
        default=[],
        dest="capability",
        help="short capability token; repeatable",
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        dest="allow",
        help="bot id allowed to call the new bot; repeatable",
    )
    return parser.parse_args(argv)




def _run_ack(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="nanobot-teams ack-unknown")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--reason", required=True, help="why you accept the remaining duplicate-execution risk")
    args = parser.parse_args(argv)
    if not Path(args.ledger).is_file():
        print("nanobot-teams: ledger does not exist", file=sys.stderr)
        return 2
    try:
        ledger = Ledger(args.ledger)
        try:
            ledger.acknowledge(args.task_id, actor=getpass.getuser(), reason=args.reason)
        finally:
            ledger.close()
    except (LedgerError, ValueError) as exc:
        print(f"nanobot-teams: {exc}", file=sys.stderr)
        return 2
    print("Target quarantine acknowledged; historical unknown outcome and charged quota are preserved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
