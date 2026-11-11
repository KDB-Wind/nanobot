"""P7 browser acceptance: drive the workbench against a live peer topology.

Boots the four-bot topology with the planner gateway serving the WebUI
(token-free websocket for the lab), seeds one writer run and one research
run so usage and a peer task exist, then drives a real browser and saves
screenshots to review/peer-comms/ui/. Exit 0 only when the drive passes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import peer_comms_probe as probe  # noqa: E402

WEBUI = probe.WT / "webui"
SHOTS = probe.EVIDENCE / "ui"


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    lab = probe.PeerLab("p7ui")
    lab.ws_requires_token = False  # browser labs connect without pairing
    rc = 1
    try:
        lab.start()
        reg = lab.register_bots()
        if reg.get("status") != "pass":
            print(json.dumps(reg, ensure_ascii=False))
            return 2
        # The browser lab stops one worker so a task addressed to it can never
        # leave the pending state: that is the row the workbench's stop
        # control acts on, and it keeps the exercise free of run races.
        lab.stop_worker("analyst")
        # One writer run via the protocol-level peer dispatch (usage + a peer
        # task the workbench can show), then one quick research run so two
        # bots have usage data.
        status, payload = lab.peer_send(
            "research", "writer", token=lab.bot_coord_tokens["research"],
            input_text="Reply with exactly OK.",
        )
        if status != 201:
            print("peer send failed", status, payload)
            return 2
        status, task = probe.http_json(
            "POST", lab.coord_url("/v1/teams/tasks"), token=lab.planner_token,
            body={"project": "peer", "bot": "research",
                  "input": "Reply with exactly OK.",
                  "operation_key": str(uuid.uuid4())},
        )
        if status != 201:
            print("planner task failed", status, task)
            return 2
        deadline = time.monotonic() + 300.0
        while time.monotonic() < deadline:
            _, tasks = probe.http_json(
                "GET", lab.coord_url("/v1/teams/tasks?limit=100"),
                token=lab.admin_token,
            )
            rows = tasks.get("tasks", [])
            if sum(1 for r in rows if r.get("run_state") == "succeeded") >= 2:
                break
            time.sleep(3.0)

        result = subprocess.run(
            [
                "node", str(ROOT / "peer_webui_drive.mjs"),
                f"http://127.0.0.1:{lab.planner_ws_port}",
                str(SHOTS),
                str(WEBUI / "package.json"),
                str(lab.coord_port),
                lab.ws_token,
                "analyst",
            ],
            cwd=str(WEBUI), capture_output=True, text=True, timeout=420,
        )
        print(result.stdout, flush=True)
        if result.stderr:
            print(result.stderr, file=sys.stderr, flush=True)
        rc = result.returncode
    finally:
        lab.teardown()
    return rc


if __name__ == "__main__":
    os.environ.setdefault("PEER_COMMS_PRESET", os.environ.get("PEER_COMMS_PRESET", "glm-5.3"))
    sys.exit(main())
