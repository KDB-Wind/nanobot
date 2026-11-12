#!/usr/bin/env python
"""Forward TCP from a non-loopback listen address to a loopback-only target.

Used by the Unix acceptance run when the guest is WSL in NAT mode: the host
proxy listens on 127.0.0.1 only, which the guest cannot reach, while the guest
has no direct egress. Run this on the Windows host, bound to the WSL gateway
address, and point the guest's http_proxy at it.

    py acceptance/relay.py --listen 172.25.240.1:17897 --target 127.0.0.1:7897

The relay moves bytes only. It keeps no state and writes no logs. Stop it with
Ctrl-C; it exits with the listener.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence


def _parse(value: str, option: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"{option} must be host:port, got {value!r}")
    return host, int(port)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, asyncio.CancelledError):
            pass


async def _handle(
    client: tuple[asyncio.StreamReader, asyncio.StreamWriter], target: tuple[str, int]
) -> None:
    reader, writer = client
    try:
        server = await asyncio.open_connection(*target)
    except OSError:
        writer.close()
        return
    upstream, down_writer = server
    done = await asyncio.gather(
        _pipe(reader, down_writer), _pipe(upstream, writer), return_exceptions=True
    )
    for outcome in done:
        if isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
            raise outcome
    down_writer.close()


async def _run(listen: tuple[str, int], target: tuple[str, int]) -> None:
    server = await asyncio.start_server(
        lambda reader, writer: _handle((reader, writer), target), listen[0], listen[1]
    )
    print(f"relay listening on {listen[0]}:{listen[1]} -> {target[0]}:{target[1]}", flush=True)
    async with server:
        await server.serve_forever()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", required=True, help="host:port to listen on")
    parser.add_argument("--target", required=True, help="host:port to forward to")
    args = parser.parse_args(argv)
    listen = _parse(args.listen, "--listen")
    target = _parse(args.target, "--target")
    try:
        asyncio.run(_run(listen, target))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
