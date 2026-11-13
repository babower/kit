"""kit-signin: OpenID Connect (Auth0 or any provider) sign-in for FastAPI apps.

See the package README for setup. The pieces:

- `oidc`: the protocol (discovery, PKCE, ID-token validation), no web framework.
- `access`: who may come in (`AllowList`, or the app's own rule), checked on every request.
- `store`: server-side sessions and pending sign-ins on the app's SQLAlchemy Base.
- `web`: cookie names, CSRF origin checks, safe redirects, security headers, log redaction.
- `app`: `SignIn`, the FastAPI routes and the `require_user` dependency.
- `testing`: `FakeIdP`, a local fake provider for the app's own tests.
"""

from .access import AccessRule, AllowList
from .app import LoginRequired, SignIn, SignInSettings, plain_message
from .oidc import AccessDenied, Identity, OidcConfig, OidcError, clear_caches
from .store import Store, Tables, define_tables
from .web import RedactQuery, SecurityHeaders

__all__ = ["AccessDenied", "AccessRule", "AllowList", "Identity", "LoginRequired", "OidcConfig",
           "OidcError", "RedactQuery", "SecurityHeaders", "SignIn", "SignInSettings", "Store",
           "Tables", "clear_caches", "define_tables", "plain_message"]
__version__ = "0.1.0"
