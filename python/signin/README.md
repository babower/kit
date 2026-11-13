# kit-signin

OpenID Connect sign-in for FastAPI apps: **Auth0**, or any standard provider (Google,
Okta...), as settings rather than code. It gives an app server-side sessions, an access
rule checked on every request, CSRF protection for forms, security headers, and a fake
provider to test against. It **fails closed**: if sign-in isn't configured, protected
pages refuse.

```bash
pip install "kit-signin @ git+https://github.com/babower/kit@signin-v0.1.0#subdirectory=python/signin"
```

Python 3.11+. Depends on FastAPI, SQLAlchemy 2 and PyJWT.

## Use it

```python
from fastapi import APIRouter, Depends, FastAPI
from kit_signin import AllowList, OidcConfig, RedactQuery, SignIn, SignInSettings, define_tables

from .db import Base, get_db           # your DeclarativeBase and session dependency

TABLES = define_tables(Base, sessions="signin_sessions", states="signin_states")

def settings() -> SignInSettings:      # read from the environment; called per request
    return SignInSettings(
        oidc=OidcConfig(issuer=env("OIDC_ISSUER"), client_id=env("OIDC_CLIENT_ID"),
                        client_secret=env("OIDC_CLIENT_SECRET"),
                        redirect_uri=env("PUBLIC_URL") + "/auth/callback"),
        production=env("ENV") == "production", cookie_prefix="myapp")

signin = SignIn(settings=settings, tables=TABLES, get_db=get_db,
                access=AllowList(lambda: env("OIDC_ALLOWED_EMAILS")))

app = FastAPI()
signin.install(app)                    # routes, security headers, redirect to sign-in
protected = APIRouter(dependencies=[Depends(signin.require_user)])

@protected.get("/")
def home(user=Depends(signin.require_user)):   # user: the session row (email, name, csrf_token)
    ...

app.include_router(protected)
logging.getLogger("uvicorn.access").addFilter(RedactQuery())
```

Create the two tables with your migrations (Alembic autogenerate picks them up from your
Base) or `Base.metadata.create_all`.

**Routes:** `GET /auth/login?next=/path` starts a sign-in, `GET /auth/callback` finishes it,
`POST /logout` (a form with `csrf_token`) ends the session. Signed-out browsers are sent to
`settings.login_path` (default `/auth/login`); API calls get 401.

**Forms:** every form must post `csrf_token` (from the session row) and come from your own
site. Use `kit_signin.web.origin_ok(request)` and `tokens_match(...)` in your own form
handlers; `/logout` already does. Inline `<style>`/`<script>` need
`nonce="{{ request.state.csp_nonce }}"` because of the Content-Security-Policy.

### Who may come in

Signing in proves who someone is; the access rule decides whether they get in. It runs at
sign-in **and on every request**, so removing someone ends their session at once.

- `AllowList(emails, require_mfa=False)`: only these addresses (a set, a comma-separated
  setting, or a function returning either). An empty list lets nobody in.
- Your own rule: any object with `admit(identity, db)` (raise `AccessDenied` to refuse) and
  `still_allowed(session, db) -> bool`. For example, admin approval:

```python
class Approval:
    def admit(self, identity, db):
        user = db.get(User, identity.email)
        if user is None:
            db.add(User(email=identity.email, status="pending")); db.commit()
            raise AccessDenied("pending_approval", "Thanks! An admin needs to approve you.")
        if user.status != "approved":
            raise AccessDenied("not_approved", "Your account is not approved.")

    def still_allowed(self, session, db):
        user = db.get(User, session.email)
        return user is not None and user.status == "approved"
```

### Other ways in

An app can offer another way in, such as an emergency password: check it yourself, then
`return signin.start_session(request, db, next_path, provider="password", email=...)`. Pass
`other_login_on=lambda: ...` so the app counts as configured when only that is switched on.
Your access rule's `still_allowed` sees `session.provider` and decides about those too.

### Settings (`SignInSettings`)

| Field | Default | Meaning |
| --- | --- | --- |
| `oidc` | `None` | `OidcConfig(issuer, client_id, client_secret, redirect_uri)`; None or incomplete turns sign-in off |
| `production` | `True` | `__Host-` cookies, `Secure`, HSTS. Use `False` only for local http |
| `cookie_prefix` | `"app"` | cookies are `<prefix>_session` and `<prefix>_oidc` |
| `session_idle_hours` / `session_max_days` | 12 / 7 | a session ends after this idle time, or this age |
| `state_minutes` | 10 | how long a started sign-in may take |
| `login_path` / `after_logout` | `/auth/login` | where signed-out browsers go; where sign-out lands |

`SignIn(message=...)` renders the outcome pages (failures, "not available") in your app's
style; `allow_start=` plugs in a rate limiter for starting sign-ins.

## Test your app's sign-in

```python
from kit_signin.testing import FakeIdP
idp = FakeIdP()            # a real local provider: discovery, keys, authorize, token
code = idp.issue_code(nonce, code_challenge, redirect_uri, email="someone@example.com")
idp.alg_mode = "none"      # or forge keys, lie in discovery, fail the token endpoint...
idp.close()
```

The package's own tests (`tests/`) show the full flow and the attacks it refuses.

## Setting up Auth0

Use one Auth0 tenant for all your apps, with one **Application** per app; each app decides
who may use it with its own access rule.

1. **Applications → Create Application**, type **Regular Web Applications**.
2. On its Settings tab:
   - Allowed Callback URLs: `https://<your-app>/auth/callback` (exact, including https)
   - Allowed Logout URLs: where `after_logout` points, for example `https://<your-app>/auth/login`
   - Application Login URI: `https://<your-app>/auth/login`
3. **Who can sign in:** Authentication → Database → turn on **Disable Sign Ups**, then create
   users and mark their emails verified, or add a social connection with your own OAuth
   client. On the application's **Connections** tab, enable only what you use.
4. **MFA (recommended):** Security → Multi-factor Auth, turn on a factor and set the policy to
   **Always**; then `AllowList(..., require_mfa=True)`.
5. In your app's environment (never in the repo): the issuer (`https://<tenant-domain>/`,
   with the trailing slash), the client id, the client **secret**, and the app's public
   address.

| You see | Cause |
| --- | --- |
| Auth0 "Callback URL mismatch" | The Allowed Callback URL doesn't match `redirect_uri` exactly |
| "not allowed to use this site" | The access rule refused (check the allow-list) |
| "email address is not verified" | Mark the user's email verified in Auth0 |
| "Two-step verification is required" | MFA is required but the sign-in had none |
| "Sign-in is misconfigured" | Wrong issuer, or the provider's endpoints aren't on its own host |
| "did not start in this browser" | An old or copied link, or cookies blocked: start again |

## What is checked

- PKCE (S256), a random `state` bound to the browser by a cookie (stored hashed, single use,
  10 minutes) and a random `nonce`.
- The provider's endpoints must be on the issuer's own origin (https; http only to localhost),
  so a bad discovery document can't send your client secret elsewhere.
- ID tokens must be RS256, signed by a published key, with the right issuer and audience, a
  valid time window, our nonce and a **verified** email. `alg: none` and HS256-with-the-public-key
  forgeries are refused.
- Sessions: only a SHA-256 of the cookie token is stored; cookies are HttpOnly, SameSite=Lax,
  and `__Host-`/Secure in production; sessions end when idle, at a maximum age, on sign-out or
  on revocation, and whenever the access rule stops allowing them.
- Logs carry a short reason code for a failure, never a token, code, claim, email or secret.
