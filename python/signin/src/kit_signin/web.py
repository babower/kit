"""Web building blocks: cookie names, CSRF origin checks, safe redirects, security headers,
size-capped form reading, and a log filter that hides sign-in secrets."""

from __future__ import annotations

import hmac
import logging
import re
import secrets
from urllib.parse import parse_qs, urlparse

from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

MAX_FORM_BYTES = 8192

DEFAULT_CSP = ("default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
               "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
               "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")


def cookie_name(base: str, production: bool) -> str:
    """__Host- cookies must be Secure, Path=/ and have no Domain, which browsers enforce,
    so a sibling subdomain cannot plant one."""
    return f"__Host-{base}" if production else base


def origin_ok(request: Request) -> bool:
    """A form post must come from this very site.

    Browsers label every request with `Sec-Fetch-Site`, which page scripts cannot change;
    "same-origin" is the strongest evidence there is, and anything else (cross-site,
    same-site, none) is refused. Browsers that don't send it fall back to Origin or
    Referer, which must name this host. `Origin: null` (sent when a Referrer-Policy hides
    it) is refused, which is why SecurityHeaders uses same-origin, never no-referrer.
    """
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site:
        return fetch_site == "same-origin"
    host = request.headers.get("host", "")
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source or not host or source == "null":
        return False
    return urlparse(source).netloc == host


def new_token() -> str:
    return secrets.token_urlsafe(24)


def tokens_match(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def safe_next(value: str | None) -> str:
    """Where to go after signing in: only a path on this site, never another host."""
    if not value or len(value) > 500 or not value.startswith("/"):
        return "/"
    if value.startswith(("//", "/\\")) or any(c in value for c in "\r\n\t\\\x00"):
        return "/"
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        return "/"
    return value


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def read_form(request: Request) -> dict[str, str]:
    """A small urlencoded form, size-capped (no multipart, so no extra dependency)."""
    if not request.headers.get("content-type", "").startswith(
            "application/x-www-form-urlencoded"):
        raise HTTPException(415, "expected a form")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            raise HTTPException(413, "form too large")
    fields = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True,
                      max_num_fields=20)
    return {k: v[0] for k, v in fields.items()}


class SecurityHeaders(BaseHTTPMiddleware):
    """Headers on every response. Inline <style> and <script> must carry the per-request
    nonce in `request.state.csp_nonce`; nothing else inline is allowed.

        app.add_middleware(SecurityHeaders, production=True)
    """

    def __init__(self, app, production: bool | None = None, csp: str = DEFAULT_CSP,
                 production_fn=None):
        super().__init__(app)
        self.csp = csp
        self._production = (production_fn if production_fn is not None
                            else (lambda: bool(production)))

    async def dispatch(self, request: Request, call_next):
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        h = response.headers
        h["Content-Security-Policy"] = self.csp.format(nonce=nonce)
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        # same-origin, not no-referrer: with no-referrer, browsers send "Origin: null"
        h["Referrer-Policy"] = "same-origin"
        h["Permissions-Policy"] = ("accelerometer=(), camera=(), geolocation=(), "
                                   "gyroscope=(), microphone=(), payment=(), usb=()")
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        h["Cross-Origin-Resource-Policy"] = "same-origin"
        h.setdefault("Cache-Control", "no-store")
        if self._production():
            h["Strict-Transport-Security"] = "max-age=31536000"
        return response


class RedactQuery(logging.Filter):
    """Keeps the one-time `code` and `state` of a sign-in callback (and any token in a URL)
    out of access logs. Add it to the "uvicorn.access" logger."""

    SENSITIVE = re.compile(
        r"((?:^|[?&])(?:code|state|id_token|access_token|token)=)[^&\s]*")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self.SENSITIVE.sub(r"\1REDACTED", a) if isinstance(a, str)
                                else a for a in record.args)
        return True
