"""A small real HTTP server for testing the client: record requests, script responses."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

Reply = tuple[int, dict[str, str], dict]  # status, headers, JSON body


def ok(accepted: int = 0, duplicates: int = 0) -> Reply:
    return 200, {}, {"accepted": accepted, "duplicates": duplicates}


class MockDashboard:
    """Calls `handler(request_number, parsed_body, headers)` for each POST.

    The default handler accepts everything and remembers event ids, answering like the real
    dashboard: new events are "accepted", ones seen before are "duplicates".
    """

    def __init__(self, handler: Callable[[int, dict, dict], Reply] | None = None):
        self.requests: list[dict] = []
        self.seen: set[str] = set()
        self.handler = handler or self._accept_all
        self._lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                try:
                    parsed = json.loads(body)
                except ValueError:
                    parsed = {}
                with outer._lock:
                    n = len(outer.requests) + 1
                    outer.requests.append({"path": self.path, "body": parsed,
                                           "headers": {k.lower(): v for k, v in
                                                       self.headers.items()}})
                status, headers, payload = outer.handler(n, parsed, outer.requests[-1]["headers"])
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):  # keep test output quiet
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)

    def _accept_all(self, n: int, body: dict, headers: dict) -> Reply:
        new = dup = 0
        for e in body.get("events", []):
            key = f"{e['project']}/{e['event_id']}"
            if key in self.seen:
                dup += 1
            else:
                self.seen.add(key)
                new += 1
        return ok(new, dup)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def event_ids(self) -> list[str]:
        return [e["event_id"] for r in self.requests for e in r["body"].get("events", [])]
