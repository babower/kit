"""Sign-in for a FastAPI app: routes, the `require_user` dependency, and sessions.

    signin = SignIn(settings=lambda: SignInSettings(oidc=OidcConfig(...), production=True),
                    tables=TABLES, get_db=get_db, access=AllowList("me@example.com"))
    signin.install(app)                       # routes, security headers, login redirects
    protected = APIRouter(dependencies=[Depends(signin.require_user)])

Routes added: GET /auth/login (start), GET /auth/callback (finish), POST /logout.
An app may add its own ways in (an emergency password, say) and start a session with
`signin.start_session(...)`; its access rule then decides about those sessions too.

Fails closed: if no way in is configured, protected routes answer 503 and nobody gets in.
Logs say what failed with a short code, never a token, code, claim or provider text.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import oidc
from .access import AccessRule
from .store import Store, Tables
from .web import (
    SecurityHeaders,
    client_ip,
    cookie_name,
    origin_ok,
    read_form,
    safe_next,
    tokens_match,
)

log = logging.getLogger("kit_signin")


@dataclass(frozen=True)
class SignInSettings:
    oidc: oidc.OidcConfig | None = None  # None or incomplete: single sign-on is off
    production: bool = True  # __Host- cookies, Secure, HSTS
    cookie_prefix: str = "app"  # cookies are <prefix>_session and <prefix>_oidc
    session_idle_hours: float = 12
    session_max_days: float = 7
    state_minutes: int = 10
    login_path: str = "/auth/login"  # where a signed-out browser is sent
    after_logout: str = "/auth/login"


class LoginRequired(Exception):
    def __init__(self, next_path: str):
        self.next_path = next_path


def plain_message(request: Request, heading: str, text: str, status: int = 200,
                  link: str | None = None, link_text: str | None = None) -> HTMLResponse:
    """A minimal page for sign-in outcomes. Apps usually pass their own `message`."""
    body = f"<h1>{html.escape(heading)}</h1><p>{html.escape(text)}</p>"
    if link:
        body += f'<p><a href="{html.escape(link)}">{html.escape(link_text or "Continue")}</a></p>'
    page = ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{html.escape(heading)}</title><body>{body}</body></html>")
    return HTMLResponse(page, status_code=status)


@dataclass
class SignIn:
    settings: Callable[[], SignInSettings]
    tables: Tables
    get_db: Callable[..., Any]
    access: AccessRule
    message: Callable[..., Response] = plain_message
    # Other ways in the app offers (an emergency password, say): if one is on, the app
    # counts as configured even with single sign-on off.
    other_login_on: Callable[[], bool] = lambda: False
    # Called with the request before a sign-in starts; False answers 429. Plug in a rate
    # limiter here (each start writes a row).
    allow_start: Callable[[Request], bool] = lambda request: True
    router: APIRouter = field(init=False)
    require_user: Callable[..., Any] = field(init=False)

    def __post_init__(self) -> None:
        self.require_user = self._make_dependency()  # one object, so FastAPI caches it
        self.router = self._make_router()

    # --- state ----------------------------------------------------------------------
    def sso_on(self) -> bool:
        cfg = self.settings().oidc
        return bool(cfg and cfg.complete)

    def configured(self) -> bool:
        return self.sso_on() or self.other_login_on()

    def store(self) -> Store:
        s = self.settings()
        return Store(self.tables, idle=timedelta(hours=s.session_idle_hours),
                     max_age=timedelta(days=s.session_max_days))

    def cookie(self, kind: str) -> str:
        s = self.settings()
        return cookie_name(f"{s.cookie_prefix}_{kind}", s.production)

    def current_session(self, request: Request, db: Any) -> Any | None:
        """The live, still-allowed session for this browser, or None. A session the access
        rule no longer allows is ended on the spot."""
        store = self.store()
        row = store.lookup(db, request.cookies.get(self.cookie("session")))
        if row is not None and not self.access.still_allowed(row, db):
            store.revoke(db, row)
            log.info("session ended: access withdrawn")
            return None
        return row

    # --- the dependency ----------------------------------------------------------------
    def _make_dependency(self) -> Callable[..., Any]:
        """The FastAPI dependency `signin.require_user`: the signed-in session, or a redirect
        to sign in (GET/HEAD) or 401. 503 if no way in is configured."""
        get_db = self.get_db

        def dependency(request: Request, db: Any = Depends(get_db)) -> Any:
            if not self.configured():
                raise HTTPException(503, "sign-in is not configured")
            row = self.current_session(request, db)
            if row is None:
                if request.method in ("GET", "HEAD"):
                    raise LoginRequired(request.url.path)
                raise HTTPException(401, "sign in first")
            return row

        return dependency

    # --- sessions ------------------------------------------------------------------------
    def start_session(self, request: Request, db: Any, next_path: str, *,
                      provider: str = "oidc", subject: str = "", email: str = "",
                      name: str = "") -> Response:
        """Create a session and redirect to `next_path` with the session cookie set."""
        s = self.settings()
        _row, token = self.store().create(db, client_ip(request),
                                          request.headers.get("user-agent", ""),
                                          provider=provider, subject=subject, email=email,
                                          name=name)
        response = RedirectResponse(safe_next(next_path), status_code=303)
        response.set_cookie(self.cookie("session"), token,
                            max_age=int(s.session_max_days * 86400), path="/",
                            secure=s.production, httponly=True, samesite="lax")
        return response

    # --- routes ----------------------------------------------------------------------------
    def _failed(self, request: Request, code: str, text: str, status: int = 400) -> Response:
        log.warning("sign-in failed: %s", code)  # the code only, never secrets or claims
        response = self.message(request, "Could not sign you in", text, status=status,
                                link=self.settings().login_path, link_text="Try again")
        response.delete_cookie(self.cookie("oidc"), path="/")
        return response

    def _make_router(self) -> APIRouter:
        router = APIRouter()
        get_db = self.get_db

        @router.get("/auth/login")
        def sso_start(request: Request, db: Any = Depends(get_db), next: str = "/") -> Response:
            if not self.sso_on():
                return self.message(request, "Not available", "Single sign-on is not set up.",
                                    status=404)
            if not self.allow_start(request):
                return self.message(request, "Slow down",
                                    "Too many sign-in attempts. Wait a minute.", status=429)
            s = self.settings()
            try:
                provider = oidc.get_provider(s.oidc)
            except oidc.OidcError as e:
                return self._failed(request, e.code, e.user_message, status=502)
            state, nonce, verifier = oidc.new_state()
            self.store().save_state(db, state, nonce, verifier, safe_next(next),
                                    timedelta(minutes=s.state_minutes))
            response = RedirectResponse(oidc.authorize_url(provider, s.oidc, state, nonce,
                                                           verifier), status_code=303)
            # Lax, not Strict: the provider sends the browser back from another site, and a
            # Strict cookie would not come with it.
            response.set_cookie(self.cookie("oidc"), state, max_age=s.state_minutes * 60,
                                path="/", secure=s.production, httponly=True, samesite="lax")
            return response

        @router.get("/auth/callback")
        def sso_callback(request: Request, db: Any = Depends(get_db), code: str = "",
                         state: str = "", error: str = "") -> Response:
            if not self.sso_on():
                return self.message(request, "Not available", "Single sign-on is not set up.",
                                    status=404)
            s = self.settings()
            if not (state and tokens_match(request.cookies.get(self.cookie("oidc")), state)):
                return self._failed(request, "state_cookie_mismatch",
                                    "That sign-in did not start in this browser. "
                                    "Please start again.")
            pending = self.store().take_state(db, state)  # single use, whatever happens next
            if pending is None:
                return self._failed(request, "state_unknown", "That sign-in link was already "
                                    "used or has expired. Start again.")
            nonce, verifier, next_path, expired = pending
            if expired:
                return self._failed(request, "state_expired",
                                    "That sign-in took too long. Start again.")
            if error:  # the provider's refusal; its description is untrusted, never shown
                return self._failed(request, f"provider_error:{error[:40]}",
                                    "The sign-in provider did not complete the sign-in.",
                                    status=403)
            if not code or len(code) > 2000:
                return self._failed(request, "no_code",
                                    "The sign-in provider did not send a code.")
            try:
                provider = oidc.get_provider(s.oidc)
                token = oidc.exchange_code(provider, s.oidc, code, verifier)
                claims = oidc.validate_id_token(provider, s.oidc, token, nonce)
                who = oidc.verified_identity(claims)
                self.access.admit(who, db)
            except oidc.AccessDenied as e:
                return self._failed(request, e.code, e.user_message, status=403)
            except oidc.OidcError as e:
                status = 502 if e.code.startswith("provider_unreachable") else 400
                return self._failed(request, e.code, e.user_message, status=status)
            log.info("sso sign-in ok")
            response = self.start_session(request, db, next_path, provider="oidc",
                                          subject=who.subject, email=who.email, name=who.name)
            response.delete_cookie(self.cookie("oidc"), path="/")
            return response

        @router.post("/logout")
        def logout(request: Request, db: Any = Depends(get_db),
                   form: dict[str, str] = Depends(read_form)) -> Response:
            """Public on purpose: it must work for a session that is already invalid. It only
            ever ends the caller's own session, and only with the CSRF token."""
            store = self.store()
            row = store.lookup(db, request.cookies.get(self.cookie("session")))
            if row is not None:
                if not (origin_ok(request) and tokens_match(form.get("csrf_token"),
                                                            row.csrf_token)):
                    return self.message(request, "Please try again", "That form did not come "
                                        "from this site. Reload the page and try again.",
                                        status=403, link="/", link_text="Back")
                store.revoke(db, row)
            response = RedirectResponse(self.settings().after_logout, status_code=303)
            response.delete_cookie(self.cookie("session"), path="/")
            return response

        return router

    # --- wiring ----------------------------------------------------------------------------
    def install(self, app: FastAPI, *, headers: bool = True) -> None:
        """Add the routes, the redirect-to-sign-in behaviour and (unless the app adds its own
        or passes headers=False) the security headers."""
        if headers:
            app.add_middleware(SecurityHeaders,
                               production_fn=lambda: self.settings().production)
        app.include_router(self.router)

        @app.exception_handler(LoginRequired)
        async def _login_required(request: Request, exc: LoginRequired) -> Response:
            target = f"{self.settings().login_path}?next={quote(exc.next_path, safe='/')}"
            return RedirectResponse(target, status_code=303)
