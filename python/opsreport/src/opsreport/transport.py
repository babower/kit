"""One HTTP POST, standard library only. Never logs the token or the body."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ._version import __version__

USER_AGENT = f"opsreport/{__version__}"  # the dashboard can show which version a project runs
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> dict:
        try:
            data = json.loads(self.body or b"{}")
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}


def events_url(base: str) -> str | None:
    """The ingest URL for a base such as https://host or https://host/v1/events.

    Refuses plain http to anything but localhost: the bearer token would cross the network
    unencrypted.
    """
    parsed = urlparse(base.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if parsed.scheme == "http" and parsed.hostname not in LOCAL_HOSTS:
        return None
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1/events"):
        path += "/v1/events"
    return parsed._replace(path=path, query="", fragment="").geturl()


def post(url: str, token: str, payload: bytes, timeout: float) -> Response:
    """POST JSON. Returns the response for ANY HTTP status; raises OSError for network
    problems (connection refused, DNS failure, timeout)."""
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except OSError:
            pass
        return Response(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body)
    except urllib.error.URLError as e:
        raise OSError(str(e.reason)) from None
