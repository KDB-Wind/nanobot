from __future__ import annotations

import concurrent.futures
import http.client
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HELPER = Path(__file__).parents[1] / "acceptance" / "peer_evidence.py"
spec = importlib.util.spec_from_file_location("peer_acceptance_evidence", HELPER)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
ReplyAckGate = module.ReplyAckGate


def test_ack_barrier_forwards_to_inbox_before_releasing_coordinator_response():
    committed: list[str] = []

    class Inbox(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            committed.append(body["delivery_id"])
            result = json.dumps({"delivery_id": body["delivery_id"], "state": "received"}).encode()
            self.send_response(202)
            self.send_header("Content-Length", str(len(result)))
            self.end_headers()
            self.wfile.write(result)

    inbox = ThreadingHTTPServer(("127.0.0.1", 0), Inbox)
    thread = threading.Thread(target=inbox.serve_forever, daemon=True)
    thread.start()
    gate = ReplyAckGate(f"http://127.0.0.1:{inbox.server_port}")
    gate.arm("workflow-1", lose_ack=True)
    gate.delivery_allowed.set()

    def post():
        conn = http.client.HTTPConnection("127.0.0.1", gate.server.server_port, timeout=5)
        try:
            conn.request("POST", "/v1/agent-inbox", json.dumps(
                {"workflow_id": "workflow-1", "delivery_id": "delivery-1"}))
            response = conn.getresponse()
            response.read()
            return response.status
        finally:
            conn.close()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(post)
            try:
                assert gate.receipt_committed.wait(3)
                assert committed == ["delivery-1"]
                assert not pending.done()
                assert gate.delivery_id == "delivery-1"
            finally:
                gate.ack_allowed.set()
            assert pending.result(timeout=3) == 202
        assert post() == 202
        assert gate.attempts == ["delivery-1", "delivery-1"]
    finally:
        gate.close()
        inbox.shutdown()
        inbox.server_close()
        thread.join(timeout=3)
