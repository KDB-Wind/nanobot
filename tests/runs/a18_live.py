"""A18 live: a real gateway user turn revokes an older workflow's grant.

Sequence: send user turn 1 over the real WebSocket, mint a workflow grant
directly into the gateway's session database (what the trusted MCP meta hook
does during a conversation), send user turn 2, then assert auto_continue
flipped to 0 in the durable tables.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "nanobot-teams" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

WS_URL = "ws://127.0.0.1:18943"
DB = Path(sys.argv[1])
SESSION_KEY = "websocket:webui-default"


async def send_on(ws, content: str) -> None:
    await ws.send(json.dumps({
        "type": "message",
        "chat_id": "webui-default",
        "turn_id": uuid.uuid4().hex,
        "content": content,
    }))
    # Wait for the turn to finish (turn_end or completion event).
    deadline = asyncio.get_event_loop().time() + 180
    while asyncio.get_event_loop().time() < deadline:
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=90))
        if frame.get("event") in ("turn_end", "turn_complete"):
            return
        if frame.get("event") == "error":
            raise RuntimeError(f"gateway error: {frame}")
    raise TimeoutError("turn did not finish")


def mint_workflow() -> None:
    import sqlite3

    conn = sqlite3.connect(DB, timeout=10)
    try:
        row = conn.execute(
            "SELECT generation FROM sessions WHERE key=?", (SESSION_KEY,)
        ).fetchone()
        assert row is not None, "session must exist after turn 1"
        generation = row[0]
        token = os.environ.get("GW_MINT_TOKEN", "minted-token")
        import hashlib

        digest = hashlib.sha256(token.encode()).hexdigest()
        now = datetime.now(tz=timezone.utc)
        conn.execute(
            "INSERT OR REPLACE INTO reply_capabilities (capability_id, digest, session_key, "
            "session_generation, workflow_id, operation_key, bot_id, instance_id, principal_id, "
            "auto_continue, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,1,?,?)",
            ("cap_live", digest, SESSION_KEY, generation, "wf-live", "*", "*", "*",
             "teams-coordinator", now.isoformat(),
             (now + timedelta(days=30)).isoformat()),
        )
        conn.execute(
            "INSERT OR REPLACE INTO external_workflows (workflow_id, session_key, "
            "session_generation, created_at, auto_continue) VALUES (?,?,?,?,1)",
            ("wf-live", SESSION_KEY, generation, now.isoformat()),
        )
        conn.commit()
        print("minted wf-live for generation", generation[:8])
    finally:
        conn.close()


def read_grant() -> int:
    import sqlite3

    conn = sqlite3.connect(DB, timeout=10)
    try:
        row = conn.execute(
            "SELECT auto_continue FROM external_workflows WHERE workflow_id='wf-live'"
        ).fetchone()
        return -1 if row is None else int(row[0])
    finally:
        conn.close()


async def main() -> int:
    token = os.environ["GW_WS_TOKEN"]
    async with websockets.connect(f"{WS_URL}?token={token}", max_size=2**23) as ws:
        await ws.send(json.dumps({"type": "attach", "chat_id": "webui-default"}))
        deadline = asyncio.get_event_loop().time() + 10
        while asyncio.get_event_loop().time() < deadline:
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if frame.get("event") in ("ready", "attached"):
                break
        await send_on(ws, "Say OK1 and nothing else.")
        mint_workflow()
        before = read_grant()
        print("grant before turn 2:", before)
        await send_on(ws, "Say OK2 and nothing else.")
    after = read_grant()
    print("grant after turn 2:", after)
    if before == 1 and after == 0:
        print("A18 LIVE PASS: the real user turn revoked the workflow grant")
        return 0
    print("A18 LIVE FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
