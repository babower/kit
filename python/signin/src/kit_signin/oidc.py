"""OpenID Connect sign-in (Authorization Code flow with PKCE), for any standard provider.

Auth0 is the first provider, but nothing here is Auth0-specific: the provider is settings
(issuer, client id and secret, and the app's callback address), so Google, Okta or another
tenant is a settings change. This module has no web framework in it; `kit_signin.app`
wires it into FastAPI.

What is checked, and why:

- The provider's endpoints come from its discovery document, and the token endpoint, the
  authorization endpoint and the signing keys must all sit on the issuer's own origin (https,
  or http to localhost for tests). A hostile or misconfigured document cannot send our client
  secret, or our trust in a key set, somewhere else.
- PKCE (S256), a random `state` bound to the browser by a cookie, and a random `nonce`
  echoed inside the ID token, so a stolen or replayed authorization code is useless.
- The ID token must be RS256 (so `alg: none` and the HS256-with-the-public-key trick are
  refused), signed by one of the provider's published keys, with the right issuer, our
  audience, a valid time window, the nonce we sent, and a verified email.
- Who may come in is a separate decision, made by the app's access rule (`kit_signin.access`).

Errors carry a short `code` for logs and a plain `user_message`; neither contains tokens,
codes, secrets or anything the provider sent.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import jwt

HTTP_TIMEOUT = 5.0
MAX_RESPONSE_BYTES = 1_000_000
DISCOVERY_TTL = 3600.0
LEEWAY_SECONDS = 30
ALLOWED_ALGORITHMS = ["RS256"]
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class OidcError(Exception):
    """A sign-in step failed. `code` is for the logs, `user_message` is safe to show."""

    def __init__(self, code: str, user_message: str = "Sign-in failed. Please try again."):
        super().__init__(code)
        self.code = code
        self.user_message = user_message


class AccessDenied(OidcError):
    pass


@dataclass(frozen=True)
class Provider:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


@dataclass(frozen=True)
class Identity:
    subject: str
    email: str
    name: str
    mfa: bool = False  # the provider says a second factor was used (amr contains "mfa")


@dataclass(frozen=True)
class OidcConfig:
    """Where the provider is and who we are. All four values come from the app's settings;
    `redirect_uri` is this app's own callback address (https://<app>/auth/callback)."""

    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scope: str = "openid email profile"

    @property
    def complete(self) -> bool:
        return bool(self.issuer and self.client_id and self.client_secret and self.redirect_uri)


def parse_emails(raw: str) -> set[str]:
    """An allow-list from a setting: separated by commas, semicolons, spaces or new lines."""
    raw = (raw or "").replace(";", ",").replace("\n", ",").replace(" ", ",")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


# --- talking to the provider -------------------------------------------------------------

def _origin(url: str) -> tuple[str, str] | None:
    p = urllib.parse.urlparse(url)
    if not p.hostname or p.scheme not in ("https", "http"):
        return None
    if p.scheme == "http" and p.hostname not in LOCAL_HOSTS:
        return None  # never talk plain http to a real host
    return p.scheme, p.netloc


def _read(resp) -> bytes:
    data = resp.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise OidcError("response_too_large")
    return data


def _get_json(url: str) -> dict:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(_read(resp))
    except OidcError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise OidcError(f"provider_unreachable:{type(e).__name__}",
                        "The sign-in provider could not be reached. Try again shortly.") from None


def _post_form(url: str, fields: dict[str, str]) -> tuple[int, dict]:
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.status, json.loads(_read(resp))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(_read(e))
        except (OidcError, ValueError, OSError):
            return e.code, {}
    except OidcError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise OidcError(f"provider_unreachable:{type(e).__name__}",
                        "The sign-in provider could not be reached. Try again shortly.") from None


_lock = threading.Lock()
_provider_cache: dict[str, tuple[float, Provider]] = {}
_jwks_clients: dict[str, jwt.PyJWKClient] = {}


def clear_caches() -> None:
    with _lock:
        _provider_cache.clear()
        _jwks_clients.clear()


def get_provider(s: OidcConfig) -> Provider:
    """The provider's endpoints from its discovery document (cached for an hour)."""
    key = s.issuer
    with _lock:
        hit = _provider_cache.get(key)
        if hit and time.monotonic() - hit[0] < DISCOVERY_TTL:
            return hit[1]
    issuer_origin = _origin(s.issuer)
    if issuer_origin is None:
        raise OidcError("bad_issuer_setting", "Sign-in is misconfigured.")
    doc = _get_json(s.issuer.rstrip("/") + "/.well-known/openid-configuration")
    if str(doc.get("issuer", "")).rstrip("/") != s.issuer.rstrip("/"):
        raise OidcError("issuer_mismatch", "Sign-in is misconfigured.")
    endpoints = {}
    for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        url = str(doc.get(name, ""))
        if _origin(url) != issuer_origin:  # same scheme and host as the issuer, or refuse
            raise OidcError(f"endpoint_off_origin:{name}", "Sign-in is misconfigured.")
        endpoints[name] = url
    provider = Provider(issuer=str(doc["issuer"]), **endpoints)
    with _lock:
        _provider_cache[key] = (time.monotonic(), provider)
    return provider


def _jwks_client(provider: Provider) -> jwt.PyJWKClient:
    with _lock:
        client = _jwks_clients.get(provider.jwks_uri)
        if client is None:
            client = jwt.PyJWKClient(provider.jwks_uri, cache_keys=True, lifespan=3600,
                                     timeout=HTTP_TIMEOUT)
            _jwks_clients[provider.jwks_uri] = client
        return client


# --- the flow --------------------------------------------------------------------------------

def new_state() -> tuple[str, str, str]:
    """(state, nonce, code_verifier): three independent random values."""
    return secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(48)


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize_url(provider: Provider, s: OidcConfig, state: str, nonce: str, verifier: str) -> str:
    query = urllib.parse.urlencode({
        "response_type": "code", "client_id": s.client_id, "redirect_uri": s.redirect_uri,
        "scope": s.scope, "state": state, "nonce": nonce,
        "code_challenge": code_challenge(verifier), "code_challenge_method": "S256"})
    return f"{provider.authorization_endpoint}?{query}"


def exchange_code(provider: Provider, s: OidcConfig, code: str, verifier: str) -> str:
    """Trade the authorization code for an ID token. Returns the raw id_token."""
    status, body = _post_form(provider.token_endpoint, {
        "grant_type": "authorization_code", "code": code, "redirect_uri": s.redirect_uri,
        "client_id": s.client_id, "client_secret": s.client_secret,
        "code_verifier": verifier})
    if status != 200 or not isinstance(body.get("id_token"), str):
        raise OidcError(f"token_exchange_failed:{status}:{str(body.get('error', ''))[:40]}")
    return body["id_token"]


_JWT_ERRORS = {
    jwt.ExpiredSignatureError: "token_expired",
    jwt.InvalidAudienceError: "bad_audience",
    jwt.InvalidIssuerError: "bad_issuer",
    jwt.InvalidSignatureError: "bad_signature",
    jwt.MissingRequiredClaimError: "missing_claim",
    jwt.ImmatureSignatureError: "token_not_yet_valid",
    jwt.InvalidIssuedAtError: "bad_issued_at",
}


def validate_id_token(provider: Provider, s: OidcConfig, token: str, nonce: str) -> dict:
    try:
        if jwt.get_unverified_header(token).get("alg") not in ALLOWED_ALGORITHMS:
            raise OidcError("bad_algorithm")  # also refuses "none" and symmetric algorithms
        key = _jwks_client(provider).get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token, key, algorithms=ALLOWED_ALGORITHMS, audience=s.client_id,
            issuer=provider.issuer, leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]})
    except OidcError:
        raise
    except jwt.PyJWKClientError as e:
        raise OidcError(f"key_lookup_failed:{type(e).__name__}") from None
    except jwt.PyJWTError as e:
        raise OidcError(_JWT_ERRORS.get(type(e), f"invalid_token:{type(e).__name__}")) from None
    aud = claims.get("aud")
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != s.client_id:
        raise OidcError("bad_authorized_party")
    sent = claims.get("nonce")
    if not isinstance(sent, str) or not hmac.compare_digest(sent.encode(), nonce.encode()):
        raise OidcError("bad_nonce")
    return claims


def verified_identity(claims: dict) -> Identity:
    """Who this is, from validated claims. Refuses a missing or unverified email."""
    email = str(claims.get("email") or "").strip().lower()
    if not email or claims.get("email_verified") is not True:
        raise AccessDenied("email_not_verified",
                           "Your email address is not verified with the sign-in provider.")
    amr = claims.get("amr") or []
    return Identity(subject=str(claims["sub"])[:255], email=email[:320],
                    name=str(claims.get("name") or "")[:200],
                    mfa="mfa" in amr if isinstance(amr, list) else False)
