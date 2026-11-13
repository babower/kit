# Changelog: kit-signin

## 0.1.0 (2026-11-13)

First release, moved out of ops-dashboard (where it was built and reviewed) so other
FastAPI apps can use it, smart-home next.

- OpenID Connect sign-in with PKCE, state and nonce; RS256 ID tokens checked against the
  provider's published keys; discovery endpoints pinned to the issuer's origin.
- Server-side sessions (hashed tokens, idle and absolute limits, revocation) and single-use
  pending sign-ins, as SQLAlchemy tables on the app's own Base, with names it chooses.
  Works on Postgres and SQLite.
- Access rules checked at sign-in and on every request: `AllowList` (optionally requiring
  MFA), or the app's own rule (for example admin approval).
- FastAPI: `/auth/login`, `/auth/callback`, `/logout`, the `require_user` dependency,
  redirect-to-sign-in, and `start_session` for an app's own ways in. Fails closed.
- Security headers with a CSP nonce, CSRF origin checks, safe redirects, an access-log
  filter that hides `code` and `state`.
- `kit_signin.testing.FakeIdP`: a local fake provider that can misbehave on purpose.
