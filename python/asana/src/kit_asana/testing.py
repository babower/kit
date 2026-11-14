"""FakeAsana: a local Asana for an app's tests. Real HTTP on 127.0.0.1, the same JSON
shapes as Asana for tasks, sections, projects and comments.

    fake = FakeAsana()                       # token "test-token" by default
    project, inbox = fake.add_project("Home"), fake.last_section
    client = Client(fake.token, base_url=fake.base_url, sleep=lambda s: None)
    ...
    fake.complete(gid)                       # as if done by a person in Asana
    fake.delete(gid)
    fake.fail_next(503, times=2)             # or fail_next(429, retry_after="1")
    fake.close()
"""

from __future__ import annotations

import itertools
import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TASK_FIELDS = ("gid", "name", "notes", "due_on", "completed", "completed_at", "created_at",
               "modified_at", "memberships", "projects", "resource_type")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class FakeAsana:
    def __init__(self, token: str = "test-token"):
        self.token = token
        self.tasks: dict[str, dict] = {}
        self.stories: dict[str, list[str]] = {}
        self.sections: dict[str, str] = {}  # section gid -> project gid
        self.projects: set[str] = set()
        self.requests: list[tuple[str, str]] = []  # (method, path) of every request
        self.bodies: list[dict] = []  # JSON bodies received, in order
        self.page_size = 100
        self._faults: list[tuple[int, str | None]] = []
        self._ids = itertools.count(1_200_000_000_000_001)
        self.last_section = ""
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status: int, body: dict | None = None,
                      headers: dict[str, str] | None = None):
                data = json.dumps(body or {}).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method: str):
                url = urlparse(self.path)
                outer.requests.append((method, url.path))
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                if body:
                    outer.bodies.append(body)
                if self.headers.get("Authorization") != f"Bearer {outer.token}":
                    return self._send(401, {"errors": [{"message": "Not Authorized"}]})
                with outer._lock:
                    if outer._faults:
                        status, retry_after = outer._faults.pop(0)
                        return self._send(status, {"errors": [{"message": "injected"}]},
                                          {"Retry-After": retry_after} if retry_after else None)
                    status, out = outer._route(method, url.path, parse_qs(url.query),
                                               body.get("data") or {})
                return self._send(status, out)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PUT(self):
                self._handle("PUT")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}/api/1.0"
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # --- setup and "a person did this in Asana" -------------------------------------
    def add_project(self, name: str = "Project", sections: tuple[str, ...] = ("Inbox",)) -> str:
        gid = str(next(self._ids))
        self.projects.add(gid)
        for _ in sections:
            self.last_section = str(next(self._ids))
            self.sections[self.last_section] = gid
        return gid

    def add_task(self, project: str, name: str, notes: str = "", section: str = "") -> str:
        """A task created by a person, not by the app."""
        return self._create({"name": name, "notes": notes,
                             "memberships": [{"project": project, "section": section}]
                             if section else [], "projects": [project]})["gid"]

    def complete(self, gid: str, when: str | None = None) -> None:
        t = self.tasks[gid]
        t["completed"], t["completed_at"] = True, when or _now()
        t["modified_at"] = _now()

    def reopen(self, gid: str) -> None:
        t = self.tasks[gid]
        t["completed"], t["completed_at"], t["modified_at"] = False, None, _now()

    def delete(self, gid: str) -> None:
        self.tasks.pop(gid, None)

    def fail_next(self, status: int, times: int = 1, retry_after: str | None = None) -> None:
        self._faults.extend([(status, retry_after)] * times)

    # --- the API ---------------------------------------------------------------------
    def _create(self, data: dict) -> dict:
        gid = str(next(self._ids))
        memberships = []
        projects = list(data.get("projects") or [])
        for m in data.get("memberships") or []:
            p, s = m.get("project"), m.get("section")
            memberships.append({"project": {"gid": p}, "section": {"gid": s} if s else None})
            projects.append(p)
        now = _now()
        task = {"gid": gid, "resource_type": "task", "name": data.get("name", ""),
                "notes": data.get("notes", ""), "due_on": data.get("due_on"),
                "completed": bool(data.get("completed", False)), "completed_at": None,
                "created_at": now, "modified_at": now, "memberships": memberships,
                "projects": [{"gid": p} for p in dict.fromkeys(projects)]}
        self.tasks[gid] = task
        return task

    def _route(self, method: str, path: str, query: dict[str, list[str]],
               data: dict) -> tuple[int, dict]:
        parts = [p for p in path.split("/") if p][2:]  # drop "api", "1.0"
        if parts == ["tasks"] and method == "POST":
            projects = set(data.get("projects") or []) | {
                m.get("project") for m in data.get("memberships") or []}
            if not projects or not projects <= self.projects:
                return 400, {"errors": [{"message": "unknown project"}]}
            for m in data.get("memberships") or []:
                if m.get("section") and self.sections.get(m["section"]) != m.get("project"):
                    return 400, {"errors": [{"message": "unknown section"}]}
            return 201, {"data": self._create(data)}
        if parts == ["tasks"] and method == "GET":
            return self._list(query)
        if len(parts) >= 2 and parts[0] == "tasks":
            task = self.tasks.get(parts[1])
            if task is None:
                return 404, {"errors": [{"message": "Not found"}]}
            if len(parts) == 3 and parts[2] == "stories" and method == "POST":
                self.stories.setdefault(task["gid"], []).append(data.get("text", ""))
                return 201, {"data": {"gid": str(next(self._ids)), "text": data.get("text")}}
            if len(parts) == 2 and method == "GET":
                return 200, {"data": task}
            if len(parts) == 2 and method == "PUT":
                for key in ("name", "notes", "due_on"):
                    if key in data:
                        task[key] = data[key]
                if "completed" in data:
                    task["completed"] = bool(data["completed"])
                    task["completed_at"] = _now() if task["completed"] else None
                task["modified_at"] = _now()
                return 200, {"data": task}
        return 404, {"errors": [{"message": "No route"}]}

    def _list(self, query: dict[str, list[str]]) -> tuple[int, dict]:
        project = (query.get("project") or [""])[0]
        if project not in self.projects:
            return 400, {"errors": [{"message": "unknown project"}]}
        rows = [t for t in self.tasks.values() if {"gid": project} in t["projects"]]
        since = (query.get("modified_since") or [None])[0]
        if since:
            rows = [t for t in rows if _parse(t["modified_at"]) >= _parse(since)]
        done_since = (query.get("completed_since") or [None])[0]
        if done_since == "now":
            rows = [t for t in rows if not t["completed"]]
        start = int((query.get("offset") or ["0"])[0])
        limit = min(int((query.get("limit") or [self.page_size])[0]), self.page_size)
        page = rows[start:start + limit]
        nxt = ({"offset": str(start + limit)} if start + limit < len(rows) else None)
        return 200, {"data": page, "next_page": nxt}


__all__ = ["TASK_FIELDS", "FakeAsana"]
