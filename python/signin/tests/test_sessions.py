"""Sessions, the per-request access check, other ways in, sign-out, and the web helpers."""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from conftest import ORIGIN, SESSION, TABLES, Base, World, sign_in
from sqlalchemy import String, update
from sqlalchemy.orm import Mapped, mapped_column

from kit_signin import AccessDenied, RedactQuery
from kit_signin.web import cookie_name, safe_next


def csrf(client):
    page = client.get("/").text
    return page.split('name="csrf_token" value="')[1].split('"')[0]


# --- failing closed ----------------------------------------------------------------------------

def test_nothing_configured_refuses_everything(tmp_path):
    world = World(tmp_path, None)
    c = world.client()
    assert c.get("/").status_code == 503
    assert c.post("/action").status_code == 503
    assert c.get("/auth/login").status_code == 404


def test_an_incomplete_provider_setting_counts_as_off(world, c):
    world.configure(oidc=world.settings.oidc.__class__("", "id", "secret", "https://x/cb"))
    assert c.get("/").status_code == 503


def test_signed_out_browsers_are_sent_to_sign_in_and_api_calls_get_401(c):
    r = c.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/auth/login?next=/"
    assert c.post("/action").status_code == 401


def test_the_sign_in_path_is_configurable(world, c):
    world.configure(login_path="/login")
    assert c.get("/").headers["location"] == "/login?next=/"


# --- the access rule is checked on every request ----------------------------------------------

def test_removing_someone_from_the_list_ends_their_session_at_once(world, c, idp):
    sign_in(c, idp)
    assert c.get("/").status_code == 200
    world.allowed = "second@example.com"
    assert c.get("/").status_code == 303
    (row,) = world.sessions()
    assert row.revoked_at is not None  # ended, not just refused
    world.allowed = "owner@example.com"  # putting them back does not revive that session
    assert c.get("/").status_code == 303


class User(Base):
    """An app's own users table with admin approval, as smart-home has."""

    __tablename__ = "demo_users"
    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")


class Approval:
    """Sign-in creates a pending user; only approved users get in, checked every request."""

    def admit(self, identity, db):
        user = db.get(User, identity.email)
        if user is None:
            db.add(User(email=identity.email, status="pending"))
            db.commit()
            raise AccessDenied("pending_approval", "Thanks! An admin needs to approve you.")
        if user.status != "approved":
            raise AccessDenied("not_approved", "Your account is not approved.")

    def still_allowed(self, session, db):
        user = db.get(User, session.email)
        return user is not None and user.status == "approved"


@pytest.fixture
def approval_world(world):
    Base.metadata.create_all(world.engine)
    world.signin.access = Approval()
    return world


def test_an_app_rule_can_require_approval(approval_world, idp):
    world, c = approval_world, approval_world.client()
    r = sign_in(c, idp)
    assert r.status_code == 403 and "approve" in r.text and not world.sessions()
    with world.Session() as db:
        db.get(User, "owner@example.com").status = "approved"
        db.commit()
    c.cookies.clear()
    assert sign_in(c, idp).status_code == 303 and c.get("/").status_code == 200
    with world.Session() as db:  # an admin denies them later: the next request is refused
        db.get(User, "owner@example.com").status = "denied"
        db.commit()
    assert c.get("/").status_code == 303


# --- sessions --------------------------------------------------------------------------------

def _age(world, **columns):
    with world.Session() as db:
        db.execute(update(TABLES.Session).values(**columns))
        db.commit()


def test_idle_sessions_end(world, c, idp):
    sign_in(c, idp)
    _age(world, last_seen_at=datetime.now(UTC) - timedelta(hours=13))
    assert c.get("/").status_code == 303


def test_sessions_end_at_their_absolute_limit_even_when_busy(world, c, idp):
    sign_in(c, idp)
    _age(world, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert c.get("/").status_code == 303


def test_activity_keeps_a_session_alive(world, c, idp):
    sign_in(c, idp)
    _age(world, last_seen_at=datetime.now(UTC) - timedelta(hours=2))
    assert c.get("/").status_code == 200
    (row,) = world.sessions()
    seen = row.last_seen_at.replace(tzinfo=UTC) if row.last_seen_at.tzinfo is None \
        else row.last_seen_at
    assert datetime.now(UTC) - seen < timedelta(minutes=1)


def test_only_the_hash_of_the_session_token_is_stored(world, c, idp):
    sign_in(c, idp)
    token = c.cookies.get(SESSION)
    (row,) = world.sessions()
    assert token and token not in (row.token_hash, row.csrf_token)


def test_revoking_everyone_or_one_person(world, idp):
    a, b = world.client(), world.client()
    sign_in(a, idp)
    sign_in(b, idp, email="second@example.com")
    with world.Session() as db:
        assert world.signin.store().revoke_all(db, email="SECOND@example.com") == 1
    assert a.get("/").status_code == 200 and b.get("/").status_code == 303
    with world.Session() as db:
        assert world.signin.store().revoke_all(db) == 1
    assert a.get("/").status_code == 303


def test_an_app_can_add_its_own_way_in(world, idp):
    """For example an emergency password: the app checks it, then starts a session."""
    from fastapi import Request

    signin = world.signin

    @world.app.post("/password-login")
    def password_login(request: Request):
        with world.Session() as db:
            return signin.start_session(request, db, "/", provider="password",
                                        email="owner@example.com")

    c = world.client()
    r = c.post("/password-login")
    assert r.status_code == 303 and c.get("/").status_code == 200
    assert world.sessions()[0].provider == "password"


def test_cookies_are_not_secure_only_outside_production(world, c, idp):
    world.configure(production=False)
    r = sign_in(c, idp)
    cookie = next(x for x in r.headers.get_list("set-cookie") if x.startswith("demo_session="))
    assert "Secure" not in cookie and "HttpOnly" in cookie
    assert "Strict-Transport-Security" not in c.get("/").headers


# --- signing out ---------------------------------------------------------------------------------

def test_sign_out_needs_the_csrf_token_and_this_site(world, c, idp):
    sign_in(c, idp)
    token = csrf(c)
    assert c.post("/logout", data={"csrf_token": "wrong"}, headers=ORIGIN).status_code == 403
    assert c.post("/logout", data={"csrf_token": token},
                  headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.post("/logout", data={"csrf_token": token},
                  headers={"Origin": "null"}).status_code == 403
    assert c.get("/").status_code == 200  # still signed in
    r = c.post("/logout", data={"csrf_token": token}, headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 303 and c.get("/").status_code == 303


def test_sign_out_without_a_session_just_clears_the_cookie(c):
    r = c.post("/logout", data={"csrf_token": ""}, headers=ORIGIN)
    assert r.status_code == 303


# --- headers and helpers -------------------------------------------------------------------------

def test_security_headers(world, c):
    h = c.get("/").headers
    assert "nonce-" in h["Content-Security-Policy"] and "frame-ancestors 'none'" in (
        h["Content-Security-Policy"])
    assert h["Referrer-Policy"] == "same-origin"  # never no-referrer: forms would break
    assert h["X-Frame-Options"] == "DENY" and h["Cache-Control"] == "no-store"
    assert h["Strict-Transport-Security"].startswith("max-age=")


@pytest.mark.parametrize("value,expected", [
    ("/a/b?c=1", "/a/b?c=1"), (None, "/"), ("", "/"), ("https://evil.example", "/"),
    ("//evil.example", "/"), ("/\\evil", "/"), ("/x\r\nSet-Cookie: a=b", "/"),
    ("javascript:alert(1)", "/"), ("/" + "a" * 600, "/"),
])
def test_safe_next(value, expected):
    assert safe_next(value) == expected


def test_cookie_names():
    assert cookie_name("x_session", True) == "__Host-x_session"
    assert cookie_name("x_session", False) == "x_session"


def test_the_access_log_hides_the_one_time_code_and_state():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", "/auth/callback?code=SECRET1&state=SECRET2&x=ok",
                             "1.1", 303), None)
    assert RedactQuery().filter(rec) is True
    line = rec.getMessage()
    assert "SECRET1" not in line and "SECRET2" not in line and "x=ok" in line


def test_tables_use_the_names_the_app_chooses():
    assert TABLES.Session.__tablename__ == "demo_sessions"
    assert TABLES.State.__tablename__ == "demo_states"
