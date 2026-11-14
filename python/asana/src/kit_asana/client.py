"""A small Asana REST client: create, update, complete, comment on and read tasks.

Standard library only. It never logs the token, request bodies or Asana's responses, and
its error messages are safe to show: they carry an HTTP status or an error type, never
the token or the text of a task.

    from kit_asana import Client
    asana = Client(token)                       # a personal access token, from settings
    task = asana.create_task(project="120...", section="120...", name="Furnace: filter",
                             notes="...", due_on="2026-12-01")
    asana.complete_task(task["gid"])
    for t in asana.tasks_in_project("120...", modified_since=last_sync,
                                    fields=["name", "completed", "completed_at"]):
        ...
"""

from __future__ import annotations

import json
import random
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlencode, urlparse

from ._version import __version__

DEFAULT_BASE_URL = "https://app.asana.com/api/1.0"
USER_AGENT = f"kit-asana/{__version__}"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
GID = re.compile(r"^\d{1,32}$")
RETRY_AFTER_CAP = 30.0
PAGE_LIMIT = 100


class AsanaError(Exception):
    """An Asana call failed. `status` is the HTTP status (0 for a network problem). The
    message is safe to log and show: no token, no request or response text."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class AsanaAuthError(AsanaError):
    """401 or 403: the token is wrong, revoked, or can't see that object."""


class AsanaNotFound(AsanaError):
    """404: the object does not exist (for example, a task deleted in Asana)."""


def _gid(value: Any, what: str = "gid") -> str:
    text = str(value or "")
    if not GID.match(text):
        raise ValueError(f"{what} must be an Asana gid (digits only)")
    return text


def _when(value: datetime | date | str | None) -> str | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return value.isoformat()


class Client:
    """Synchronous Asana client. Thread-safe as long as each thread makes its own calls
    (it keeps no per-request state).

    Retries: every call retries 429 (honouring Retry-After up to 30 s). Reads and updates
    also retry 5xx and network errors. Creating a task or a comment does NOT retry those,
    because the first attempt may have succeeded: the caller decides (and should be
    idempotent, for example by remembering task gids).
    """

    def __init__(self, token: str, *, base_url: str = DEFAULT_BASE_URL, timeout: float = 10.0,
                 budget: float = 60.0, max_attempts: int = 4, backoff: float = 0.5,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        token = (token or "").strip()
        if not token or not token.isprintable() or " " in token:
            raise ValueError("the Asana token is empty or contains spaces or line breaks")
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or (
                parsed.scheme == "http" and parsed.hostname not in LOCAL_HOSTS):
            raise ValueError("base_url must be https (plain http only to localhost)")
        self._token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.budget = budget
        self.max_attempts = max(1, max_attempts)
        self.backoff = backoff
        self._sleep = sleep
        self._clock = clock

    def __repr__(self) -> str:  # never show the token
        return f"Client(base_url={self.base_url!r})"

    # --- tasks -----------------------------------------------------------------------
    def create_task(self, *, project: str, name: str, section: str | None = None,
                    notes: str = "", due_on: date | str | None = None,
                    **fields: Any) -> dict:
        """Create a task in a project (and a section of it). Returns the task."""
        data: dict[str, Any] = {"name": name, "notes": notes, **fields}
        if section:
            data["memberships"] = [{"project": _gid(project, "project"),
                                    "section": _gid(section, "section")}]
        else:
            data["projects"] = [_gid(project, "project")]
        if due_on is not None:
            data["due_on"] = _when(due_on)
        return self._call("POST", "/tasks", body={"data": data}, retry_unsafe=False)["data"]

    def update_task(self, gid: str, **fields: Any) -> dict:
        """Change fields of a task (name, notes, due_on, completed, ...). Returns the task."""
        if "due_on" in fields:
            fields["due_on"] = _when(fields["due_on"])
        return self._call("PUT", f"/tasks/{_gid(gid)}", body={"data": fields})["data"]

    def complete_task(self, gid: str) -> dict:
        return self.update_task(gid, completed=True)

    def get_task(self, gid: str, fields: Iterable[str] = ()) -> dict:
        query = {"opt_fields": ",".join(fields)} if fields else {}
        return self._call("GET", f"/tasks/{_gid(gid)}", query=query)["data"]

    def add_comment(self, gid: str, text: str) -> dict:
        return self._call("POST", f"/tasks/{_gid(gid)}/stories",
                          body={"data": {"text": text}}, retry_unsafe=False)["data"]

    def tasks_in_project(self, project: str, *, modified_since: datetime | str | None = None,
                         completed_since: datetime | str | None = None,
                         fields: Iterable[str] = ()) -> Iterator[dict]:
        """Every task in the project (paginated). `modified_since` limits it to tasks
        changed since then; `completed_since="now"` gives only incomplete tasks."""
        query: dict[str, Any] = {"project": _gid(project, "project"), "limit": PAGE_LIMIT}
        if fields:
            query["opt_fields"] = ",".join(fields)
        if modified_since is not None:
            query["modified_since"] = _when(modified_since)
        if completed_since is not None:
            query["completed_since"] = _when(completed_since)
        while True:
            page = self._call("GET", "/tasks", query=query)
            yield from page.get("data") or []
            nxt = (page.get("next_page") or {}).get("offset")
            if not nxt:
                return
            query["offset"] = nxt

    # --- transport -------------------------------------------------------------------
    def _call(self, method: str, path: str, *, query: dict | None = None,
              body: dict | None = None, retry_unsafe: bool = True) -> dict:
        url = self.base_url + path + ("?" + urlencode(query) if query else "")
        payload = json.dumps(body).encode() if body is not None else None
        deadline = self._clock() + self.budget
        attempt = 0
        while True:
            attempt += 1
            status, headers, raw, net_error = self._send(method, url, payload)
            if net_error is None and 200 <= status < 300:
                try:
                    data = json.loads(raw or b"{}")
                except ValueError:
                    raise AsanaError("Asana sent a response that is not JSON", status) from None
                return data if isinstance(data, dict) else {}
            if net_error is None and status in (401, 403):
                raise AsanaAuthError(f"Asana refused the token or access (HTTP {status})",
                                     status)
            if net_error is None and status == 404:
                raise AsanaNotFound("Asana object not found (HTTP 404)", 404)
            retryable = (status == 429 or (retry_unsafe and (net_error is not None
                                                              or status >= 500)))
            if not retryable or attempt >= self.max_attempts:
                break
            wait = self._wait(attempt, headers.get("retry-after") if status == 429 else None)
            if wait is None or self._clock() + wait > deadline:
                break
            self._sleep(wait)
        if net_error is not None:
            raise AsanaError(f"Asana unreachable ({net_error})", 0)
        raise AsanaError(f"Asana request failed (HTTP {status})", status)

    def _wait(self, attempt: int, retry_after: str | None) -> float | None:
        if retry_after is not None:
            try:
                seconds = float(retry_after)
            except ValueError:
                seconds = self.backoff
            return None if seconds > RETRY_AFTER_CAP else max(0.0, seconds)
        base = self.backoff * (2 ** (attempt - 1))
        return base + random.uniform(0, base / 2)

    def _send(self, method: str, url: str,
              payload: bytes | None) -> tuple[int, dict[str, str], bytes, str | None]:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json",
                   "User-Agent": USER_AGENT}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=payload, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(), None
        except urllib.error.HTTPError as e:
            try:
                e.read()
            except Exception:
                pass
            return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, b"", None
        except (urllib.error.URLError, OSError, ValueError) as e:
            return 0, {}, b"", type(e).__name__
