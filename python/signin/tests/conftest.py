"""A small app wired the way a project wires kit-signin, on SQLite (as smart-home uses)."""

from __future__ import annotations

import html
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from kit_signin import AllowList, OidcConfig, SignIn, SignInSettings, define_tables
from kit_signin.oidc import clear_caches
from kit_signin.testing import CLIENT_ID, CLIENT_SECRET, FakeIdP

ORIGIN = {"Origin": "https://testserver"}
SESSION = "__Host-demo_session"
OIDC = "__Host-demo_oidc"


class Base(DeclarativeBase):
    pass


TABLES = define_tables(Base, sessions="demo_sessions", states="demo_states")


class World:
    """One test's app, database, settings and fake provider."""

    def __init__(self, tmp_path, idp: FakeIdP | None):
        self.engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}",
                                    connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.idp = idp
        self.allowed = "owner@example.com, second@example.com"
        self.require_mfa = False
        self.settings = SignInSettings(
            oidc=OidcConfig(idp.issuer, CLIENT_ID, CLIENT_SECRET,
                            "https://testserver/auth/callback") if idp else None,
            production=True, cookie_prefix="demo")
        self.starts = 0
        self.start_limit = 1000

        def get_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        def allow_start(request):
            self.starts += 1
            return self.starts <= self.start_limit

        self.access = AllowList(lambda: self.allowed, require_mfa=lambda: self.require_mfa)
        self.signin = SignIn(settings=lambda: self.settings, tables=TABLES, get_db=get_db,
                             access=self.access, allow_start=allow_start)
        app = FastAPI()
        self.signin.install(app)
        protected = APIRouter(dependencies=[Depends(self.signin.require_user)])

        @protected.get("/", response_class=HTMLResponse)
        def home(user=Depends(self.signin.require_user)):
            return (f"<p>{html.escape(user.email)}</p>"
                    f'<input name="csrf_token" value="{user.csrf_token}">')

        @protected.post("/action")
        def action():
            return {"ok": True}

        app.include_router(protected)
        self.app = app

    def client(self) -> TestClient:
        return TestClient(self.app, base_url="https://testserver", follow_redirects=False)

    def configure(self, **changes) -> None:
        self.settings = replace(self.settings, **changes)

    def sessions(self) -> list:
        with self.Session() as db:
            return list(db.scalars(select(TABLES.Session)))


@pytest.fixture(autouse=True)
def _fresh_provider_caches():
    clear_caches()
    yield
    clear_caches()


@pytest.fixture
def idp():
    server = FakeIdP()
    yield server
    server.close()


@pytest.fixture
def world(tmp_path, idp):
    return World(tmp_path, idp)


@pytest.fixture
def c(world):
    return world.client()


def begin(client, next_="/"):
    r = client.get("/auth/login", params={"next": next_})
    assert r.status_code == 303, r.text
    q = {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}
    return {"response": r, **q}


def finish(client, idp, params, **claims):
    code = idp.issue_code(params["nonce"], params["code_challenge"], params["redirect_uri"],
                          **claims)
    return client.get("/auth/callback", params={"code": code, "state": params["state"]})


def sign_in(client, idp, **claims):
    return finish(client, idp, begin(client), **claims)
