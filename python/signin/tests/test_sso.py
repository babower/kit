"""Single sign-on, tested against a fake provider that can also misbehave on purpose.
Ported from ops-dashboard, where this sign-in was built."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from conftest import OIDC, ORIGIN, SESSION, World, begin, finish, sign_in
from sqlalchemy import select, text

from kit_signin.testing import CLIENT_ID, CLIENT_SECRET, FakeIdP, make_key


def assert_refused(world, client, response, status):
    assert response.status_code == status, response.text
    assert not world.sessions()
    assert client.get("/").status_code == 303


def states(world):
    with world.Session() as db:
        return list(db.scalars(select(world.signin.tables.State)))


# --- the happy path ------------------------------------------------------------------------

def test_signing_in(c, idp, world):
    r = sign_in(c, idp)
    assert r.status_code == 303 and r.headers["location"] == "/"
    (row,) = world.sessions()
    assert (row.provider, row.email, row.name, row.subject) == (
        "oidc", "owner@example.com", "The Owner", "auth0|user-1")
    page = c.get("/")
    assert page.status_code == 200 and "owner@example.com" in page.text
    assert not states(world)  # state used up


def test_the_provider_redirect_is_built_correctly(c, idp):
    p = begin(c)
    assert p["response"].headers["location"].startswith(idp.issuer + "authorize?")
    assert (p["response_type"], p["scope"], p["client_id"]) == (
        "code", "openid email profile", CLIENT_ID)
    assert p["code_challenge_method"] == "S256" and p["redirect_uri"] == (
        "https://testserver/auth/callback")
    assert all(len(p[k]) >= 40 for k in ("state", "nonce", "code_challenge"))
    assert "client_secret" not in p["response"].headers["location"]
    cookie = next(x for x in p["response"].headers.get_list("set-cookie") if x.startswith(OIDC))
    assert all(a in cookie for a in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"))
    assert "Domain" not in cookie


def test_each_attempt_uses_fresh_random_values(c):
    a, b = begin(c), begin(c)
    assert len({a["state"], b["state"], a["nonce"], b["nonce"]}) == 4


def test_only_the_hash_of_the_state_is_stored(c, world):
    p = begin(c)
    with world.engine.connect() as conn:
        dump = str(conn.execute(text("select * from demo_states")).all())
    assert p["state"] not in dump


def test_next_is_kept_and_unsafe_values_are_dropped(c, idp):
    assert finish(c, idp, begin(c, "/projects/demo?range=30d")).headers["location"] == (
        "/projects/demo?range=30d")
    c.cookies.clear()
    for bad in ("https://evil.example", "//evil.example", "/\\evil"):
        r = finish(c, idp, begin(c, bad))
        assert r.status_code == 303 and r.headers["location"] == "/"
        c.cookies.clear()


def test_a_session_cookie_is_issued_like_the_password_login(c, idp):
    r = sign_in(c, idp)
    cookie = next(x for x in r.headers.get_list("set-cookie") if x.startswith(SESSION))
    assert all(a in cookie for a in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"))
    assert any(x.startswith(OIDC) and "Max-Age=0" in x for x in r.headers.get_list("set-cookie"))


def test_second_login_gets_a_new_session(c, idp, world):
    sign_in(c, idp)
    first = c.cookies.get(SESSION)
    c.cookies.delete(SESSION)
    sign_in(c, idp)
    assert c.cookies.get(SESSION) != first and len(world.sessions()) == 2


def test_signing_out_works_for_sso_sessions(c, idp, world):
    sign_in(c, idp)
    token = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/").text).group(1)
    assert c.post("/logout", data={"csrf_token": token}, headers=ORIGIN).status_code == 303
    assert c.get("/").status_code == 303 and world.sessions()[0].revoked_at is not None


def test_provider_and_keys_are_fetched_once(c, idp):
    sign_in(c, idp)
    c.cookies.clear()
    sign_in(c, idp)
    assert idp.requests.count(("GET", "/.well-known/openid-configuration")) == 1
    assert idp.requests.count(("GET", "/.well-known/jwks.json")) == 1


# --- the state parameter and the browser binding -------------------------------------------

def test_a_callback_without_the_browsers_cookie_is_refused(c, idp, world):
    p = begin(c)
    c.cookies.clear()  # a different browser, or an attacker's crafted link
    assert_refused(world, c, finish(c, idp, p), 400)


def test_a_callback_with_a_different_state_is_refused(c, idp, world):
    p = begin(c)
    other = {**p, "state": "x" * 43}
    assert_refused(world, c, finish(c, idp, other), 400)


def test_a_state_works_only_once(c, idp, world):
    p = begin(c)
    code = idp.issue_code(p["nonce"], p["code_challenge"], p["redirect_uri"])
    assert c.get("/auth/callback", params={"code": code, "state": p["state"]}).status_code == 303
    c.cookies.set(OIDC, p["state"])  # an attacker replays the same callback
    code2 = idp.issue_code(p["nonce"], p["code_challenge"], p["redirect_uri"])
    r = c.get("/auth/callback", params={"code": code2, "state": p["state"]})
    assert r.status_code == 400 and len(world.sessions()) == 1


def test_an_expired_state_is_refused(c, idp, world):
    p = begin(c)
    with world.engine.begin() as conn:
        conn.execute(text("update demo_states set expires_at = :t"),
                     {"t": datetime.now(UTC) - timedelta(seconds=1)})
    assert_refused(world, c, finish(c, idp, p), 400)


def test_a_provider_refusal_is_reported_without_echoing_its_text(c, idp, world):
    p = begin(c)
    r = c.get("/auth/callback", params={"error": "access_denied", "state": p["state"],
                                        "error_description": "<script>SENTINEL</script>"})
    assert r.status_code == 403 and "SENTINEL" not in r.text
    assert not world.sessions()
    assert c.get("/auth/callback", params={"error": "x", "state": p["state"]}).status_code == 400


def test_a_missing_code_is_refused(c, world):
    p = begin(c)
    assert_refused(world, c, c.get("/auth/callback", params={"state": p["state"]}), 400)


# --- the code exchange and PKCE ------------------------------------------------------------

def test_the_token_request_carries_the_secret_and_pkce_verifier(c, idp):
    sign_in(c, idp)
    (req,) = idp.token_requests
    assert req["client_secret"] == CLIENT_SECRET and req["grant_type"] == "authorization_code"
    assert len(req["code_verifier"]) >= 43 and req["redirect_uri"] == (
        "https://testserver/auth/callback")


def test_a_code_bound_to_another_pkce_challenge_fails(c, idp, world):
    p = begin(c)
    assert_refused(world, c, finish(c, idp, {**p, "code_challenge": "forged"}), 400)


def test_a_code_cannot_be_used_twice(c, idp, world):
    p = begin(c)
    code = idp.issue_code(p["nonce"], p["code_challenge"], p["redirect_uri"])
    c.get("/auth/callback", params={"code": code, "state": p["state"]})
    c.cookies.clear()
    p2 = begin(c)
    r = c.get("/auth/callback", params={"code": code, "state": p2["state"]})  # the old code
    assert r.status_code == 400 and len(world.sessions()) == 1


# --- the ID token --------------------------------------------------------------------------

@pytest.mark.parametrize("claims", [
    {"nonce": "not-the-nonce"},
    {"iss": "https://evil.example/"},
    {"aud": "someone-elses-client"},
    {"aud": [CLIENT_ID, "other"], "azp": "other"},
    {"exp": int(datetime.now(UTC).timestamp()) - 3600},
    {"nbf": int(datetime.now(UTC).timestamp()) + 3600},
    {"sub": None},
    {"exp": None},
    {"nonce": None},
])
def test_bad_id_tokens_are_refused(c, idp, world, claims):
    assert_refused(world, c, sign_in(c, idp, **claims), 400)


def test_a_token_signed_by_an_unpublished_key_is_refused(c, idp, world):
    idp.signing_key = make_key("attacker")
    assert_refused(world, c, sign_in(c, idp), 400)


def test_a_known_key_id_with_a_different_key_is_refused(c, idp, world):
    idp.signing_key = make_key("attacker")
    idp.published = [make_key("attacker-published-as-ours")]
    assert_refused(world, c, sign_in(c, idp), 400)


@pytest.mark.parametrize("mode", ["none", "HS256"])
def test_algorithm_confusion_attacks_are_refused(c, idp, world, mode):
    idp.alg_mode = mode
    assert_refused(world, c, sign_in(c, idp), 400)


def test_a_token_response_without_an_id_token_is_refused(c, idp, world):
    idp.token_body_override = {"access_token": "x", "token_type": "Bearer"}
    assert_refused(world, c, sign_in(c, idp), 400)


def test_a_clock_skew_within_the_leeway_is_tolerated(c, idp):
    now = int(datetime.now(UTC).timestamp())
    assert sign_in(c, idp, iat=now + 20, exp=now + 320).status_code == 303


# --- who may sign in -----------------------------------------------------------------------

def test_an_email_not_on_the_list_is_refused(c, idp, world):
    r = sign_in(c, idp, email="stranger@example.com")
    assert_refused(world, c, r, 403)
    assert "not allowed" in r.text


@pytest.mark.parametrize("claims", [{"email_verified": False}, {"email_verified": "true"},
                                    {"email_verified": None}, {"email": None}, {"email": ""}])
def test_unverified_or_missing_email_is_refused(c, idp, world, claims):
    assert_refused(world, c, sign_in(c, idp, **claims), 403)


def test_the_allow_list_ignores_case_and_spacing(c, idp):
    assert sign_in(c, idp, email="  OWNER@Example.COM ").status_code == 303
    c.cookies.clear()
    assert sign_in(c, idp, email="second@example.com").status_code == 303


def test_an_empty_allow_list_lets_nobody_in(world, c, idp):
    world.allowed = ""
    assert_refused(world, c, sign_in(c, idp), 403)


def test_mfa_can_be_required(world, c, idp):
    world.require_mfa = True
    assert_refused(world, c, sign_in(c, idp, amr=["pwd"]), 403)
    assert_refused(world, c, sign_in(c, idp, amr=None), 403)
    c.cookies.clear()
    assert sign_in(c, idp, amr=["pwd", "mfa"]).status_code == 303


def test_mfa_is_not_demanded_unless_switched_on(c, idp):
    assert sign_in(c, idp, amr=["pwd"]).status_code == 303


# --- when the provider misbehaves ----------------------------------------------------------

def test_a_provider_that_is_down_gives_a_clear_error(tmp_path):
    dead = FakeIdP()
    dead.close()
    world = World(tmp_path, dead)
    c = world.client()
    r = c.get("/auth/login")
    assert r.status_code == 502 and "could not be reached" in r.text
    assert not states(world)


def test_a_token_endpoint_error_gives_a_clear_error(c, idp, world):
    idp.token_status = 500
    assert_refused(world, c, sign_in(c, idp), 400)


@pytest.mark.parametrize("override", [
    {"issuer": "https://someone-else.example/"},
    {"token_endpoint": "https://evil.example/oauth/token"},   # would receive our secret
    {"jwks_uri": "https://evil.example/keys"},               # would supply the trusted keys
    {"authorization_endpoint": "https://evil.example/authorize"},
    {"token_endpoint": None},
])
def test_a_discovery_document_that_points_elsewhere_is_refused(c, idp, world, override):
    idp.discovery_override = override
    r = c.get("/auth/login")
    assert r.status_code == 502 and "misconfigured" in r.text
    assert not states(world)


def test_plain_http_to_a_real_host_is_refused(world, c):
    world.configure(oidc=world.settings.oidc.__class__(
        "http://login.example.com/", CLIENT_ID, CLIENT_SECRET,
        "https://testserver/auth/callback"))
    assert c.get("/auth/login").status_code == 502


def test_secrets_and_tokens_never_reach_the_logs(c, idp, caplog):
    caplog.set_level("DEBUG")
    p = begin(c)
    code = idp.issue_code(p["nonce"], p["code_challenge"], p["redirect_uri"])
    idp.alg_mode = "none"  # make it fail, so the failure path logs too
    c.get("/auth/callback", params={"code": code, "state": p["state"]})
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("kit_signin"))
    assert "bad_algorithm" in ours  # it did log the failure...
    for secret in (CLIENT_SECRET, code, p["state"], p["nonce"], "owner@example.com"):
        assert secret not in ours  # ...but none of the sensitive values


