from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


class ReplyAckGate:
    """Forward to a real inbox while controlling its delivery acknowledgement."""

    def __init__(self, target: str) -> None:
        self.target = urlsplit(target)
        self.workflow = ""
        self.delivery_allowed = threading.Event()
        self.ack_allowed = threading.Event()
        self.receipt_committed = threading.Event()
        self.attempts: list[str] = []
        self.delivery_id = ""
        self.error = ""
        gate = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def do_GET(self) -> None:
                self.forward()

            def do_POST(self) -> None:
                self.forward()

            def forward(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                delivery = self.command == "POST" and self.path == "/v1/agent-inbox"
                data = json.loads(body) if delivery else {}
                controlled = delivery and data.get("workflow_id") == gate.workflow
                if controlled:
                    gate.attempts.append(str(data.get("delivery_id", "")))
                    if not gate.delivery_allowed.wait(180):
                        gate.error = "original run did not finish before reply delivery"
                        self.send_error(504)
                        return
                conn = http.client.HTTPConnection(gate.target.hostname, gate.target.port, timeout=30)
                try:
                    headers = {k: v for k, v in self.headers.items()
                               if k.lower() not in {"host", "content-length", "connection"}}
                    conn.request(self.command, self.path, body=body, headers=headers)
                    response = conn.getresponse()
                    payload = response.read()
                    status = response.status
                except OSError as exc:
                    gate.error = type(exc).__name__
                    self.send_error(502)
                    return
                finally:
                    conn.close()
                if controlled and status in (200, 202):
                    gate.delivery_id = str(data.get("delivery_id", ""))
                    gate.receipt_committed.set()
                    if not gate.ack_allowed.wait(180):
                        gate.error = "delivery acknowledgement barrier timed out"
                        self.send_error(504)
                        return
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def arm(self, workflow: str, *, lose_ack: bool) -> None:
        self.workflow = workflow
        self.delivery_allowed.clear()
        self.receipt_committed.clear()
        self.ack_allowed.clear()
        self.attempts.clear()
        self.delivery_id = ""
        self.error = ""
        if not lose_ack:
            self.ack_allowed.set()

    def close(self) -> None:
        self.delivery_allowed.set()
        self.ack_allowed.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def one_database(home: Path, name: str) -> Path:
    paths = list(home.rglob(name))
    if len(paths) != 1:
        raise ValueError(f"expected one {name} database, found {len(paths)}")
    return paths[0]


def read_rows(db: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn.execute(sql, params).fetchall()


def continuation_evidence(home: Path, task_id: str, secret: str) -> dict:
    db = one_database(home, "sessions.sqlite3")
    rows = read_rows(db, "SELECT delivery_id, session_key, session_generation, state, wake_due "
                     "FROM external_inbox WHERE task_id=?", (task_id,))
    evidence: dict = {"receipt_count": len(rows)}
    if len(rows) != 1:
        return evidence
    receipt = dict(rows[0])
    evidence.update(receipt)
    session = read_rows(db, "SELECT metadata, generation FROM sessions WHERE key=?",
                        (receipt["session_key"],))
    if len(session) != 1:
        return evidence
    metadata = json.loads(session[0]["metadata"])
    owner = metadata.get("pending_user_turn_owner", {})
    if owner.get("delivery_id") == receipt["delivery_id"]:
        evidence["accepting_turn_id"] = owner.get("turn_id", "")
    evidence["turn_pending"] = bool(metadata.get("pending_user_turn"))
    evidence["generation_matches"] = session[0]["generation"] == receipt["session_generation"]
    messages = [(r["position"], json.loads(r["record"])) for r in read_rows(
        db, "SELECT position, record FROM messages WHERE session_key=? ORDER BY position",
        (receipt["session_key"],))]
    marker = "external_delivery:" + receipt["delivery_id"]
    injected = [(pos, record) for pos, record in messages
                if record.get("external_delivery_id") == marker]
    evidence["injection_count"] = len(injected)
    evidence["continuation_count"] = 0
    if len(injected) == 1:
        position = injected[0][0]
        continuations = [(pos, record) for pos, record in messages if pos > position
                         and record.get("role") == "assistant"
                         and not record.get("tool_calls")
                         and "WAKE_ACK" in str(record.get("content", ""))
                         and secret in str(record.get("content", ""))]
        evidence["continuation_count"] = len(continuations)
        evidence["injection_position"] = position
        if continuations:
            evidence["continuation_position"] = continuations[0][0]
    return evidence


class ContinuationWatcher:
    """Observe the accepting turn while coordinator restarts block the driver."""

    def __init__(self, home: Path, task_id: str, secret: str) -> None:
        self.home = home
        self.task_id = task_id
        self.secret = secret
        self.turn_id = ""
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._observe, daemon=True)
        self._thread.start()

    def _observe(self) -> None:
        while not self._stopped.is_set():
            try:
                current = continuation_evidence(self.home, self.task_id, self.secret)
                self.turn_id = current.get("accepting_turn_id") or self.turn_id
            except (OSError, ValueError, sqlite3.Error):
                pass
            self._stopped.wait(0.02)

    def close(self) -> None:
        self._stopped.set()
        self._thread.join(timeout=5)
