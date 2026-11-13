"""A fake OpenID Connect provider (a real HTTP server on a local port) for tests.

    from kit_signin.testing import FakeIdP
    idp = FakeIdP()                      # serves on http://127.0.0.1:<port>/
    config = OidcConfig(idp.issuer, idp.client_id, idp.client_secret, redirect_uri)
    ...
    idp.close()

Needs the `testing` extra only for `cryptography`, which PyJWT[crypto] already brings.

It implements discovery, a JWKS endpoint, an authorize endpoint that signs the "user" in at
once and bounces back, and a token endpoint that enforces the client secret, the redirect
address, single-use codes and PKCE, like a real provider. Knobs on the instance let a test
make it misbehave: sign with the wrong key, use another algorithm, lie in discovery, fail.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

CLIENT_ID = "client-123"
CLIENT_SECRET = "secret-xyz-do-not-log"


@lru_cache
def make_key(name: str):
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def public_pem(key) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwk(key, kid: str) -> dict:
    nums = key.public_key().public_numbers()
    size = (nums.n.bit_length() + 7) // 8
    return {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid,
            "n": b64(nums.n.to_bytes(size, "big")), "e": b64(nums.e.to_bytes(3, "big"))}


class FakeIdP:
    def __init__(self, client_id: str = CLIENT_ID, client_secret: str = CLIENT_SECRET):
        self.client_id = client_id
        self.client_secret = client_secret
        self.key = make_key("good")             # the provider's real signing key
        self.published = [make_key("good")]      # keys listed in the JWKS
        self.signing_key = make_key("good")      # what actually signs (change to forge)
        self.kid = "test-key-1"
        self.alg_mode = "RS256"                  # RS256 | none | HS256
        self.discovery_override: dict = {}
        self.token_status = 200
        self.token_body_override: dict | None = None
        self.default_claims: dict = {}           # applied to every token (e.g. email)
        self.codes: dict[str, dict] = {}
        self.requests: list[tuple[str, str]] = []
        self.token_requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, status: int, body: dict):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                url = urlparse(self.path)
                outer.requests.append(("GET", url.path))
                if url.path == "/.well-known/openid-configuration":
                    base = outer.issuer
                    doc = {"issuer": base, "authorization_endpoint": base + "authorize",
                           "token_endpoint": base + "oauth/token",
                           "jwks_uri": base + ".well-known/jwks.json", **outer.discovery_override}
                    return self._json(200, doc)
                if url.path == "/.well-known/jwks.json":
                    return self._json(200, {"keys": [jwk(k, outer.kid) for k in outer.published]})
                if url.path == "/authorize":
                    q = {k: v[0] for k, v in parse_qs(url.query).items()}
                    code = outer.issue_code(q["nonce"], challenge=q["code_challenge"],
                                            redirect_uri=q["redirect_uri"])
                    target = q["redirect_uri"] + "?" + urlencode({"code": code,
                                                                  "state": q["state"]})
                    self.send_response(302)
                    self.send_header("Location", target)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                return self._json(404, {})

            def do_POST(self):
                outer.requests.append(("POST", self.path))
                length = int(self.headers.get("Content-Length", 0))
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                outer.token_requests.append(form)
                if self.path != "/oauth/token":
                    return self._json(404, {})
                if outer.token_status != 200:
                    return self._json(outer.token_status, {"error": "server_error"})
                return self._json(*outer.handle_token(form))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.issuer = f"http://127.0.0.1:{self.port}/"
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # --- codes and tokens ----------------------------------------------------------------
    def issue_code(self, request_nonce: str, challenge: str, redirect_uri: str = "",
                   **claims) -> str:
        code = secrets.token_urlsafe(24)
        self.codes[code] = {"nonce": request_nonce, "challenge": challenge, "claims": claims,
                            "redirect_uri": redirect_uri}
        return code

    def claims(self, request_nonce: str, **override) -> dict:
        now = int(time.time())
        base = {"iss": self.issuer, "sub": "auth0|user-1", "aud": self.client_id,
                "iat": now, "exp": now + 300, "nonce": request_nonce, "email": "owner@example.com",
                "email_verified": True, "name": "The Owner", "amr": ["pwd"]}
        base.update(self.default_claims)
        base.update(override)
        return {k: v for k, v in base.items() if v is not None}  # None removes a claim

    def handle_token(self, form: dict) -> tuple[int, dict]:
        entry = self.codes.pop(form.get("code", ""), None)  # single use
        if (entry is None or form.get("client_id") != self.client_id
                or not hmac.compare_digest(form.get("client_secret", ""), self.client_secret)
                or form.get("grant_type") != "authorization_code"):
            return 400, {"error": "invalid_grant"}
        if entry["redirect_uri"] and form.get("redirect_uri") != entry["redirect_uri"]:
            return 400, {"error": "invalid_grant"}
        challenge = b64(hashlib.sha256(form.get("code_verifier", "").encode()).digest())
        if challenge != entry["challenge"]:
            return 400, {"error": "invalid_grant", "error_description": "PKCE failed"}
        if self.token_body_override is not None:
            return 200, self.token_body_override
        claims = self.claims(entry["nonce"], **entry["claims"])
        return 200, {"access_token": "not-used", "token_type": "Bearer", "expires_in": 300,
                     "id_token": self.sign(claims)}

    def sign(self, claims: dict) -> str:
        headers = {"kid": self.kid}
        if self.alg_mode == "none":
            return jwt.encode(claims, None, algorithm="none", headers=headers)
        if self.alg_mode == "HS256":  # the classic forgery: HMAC with the PUBLIC key as secret
            head = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": self.kid}).encode())
            body = b64(json.dumps(claims).encode())
            mac = hmac.new(public_pem(self.key), f"{head}.{body}".encode(), hashlib.sha256)
            return f"{head}.{body}.{b64(mac.digest())}"
        return jwt.encode(claims, pem(self.signing_key), algorithm="RS256", headers=headers)
