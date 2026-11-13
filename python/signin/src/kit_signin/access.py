"""Who may come in. Signing in only proves who someone is; the app's access rule decides
whether they get in, at sign-in and again on every request (so removing someone takes
effect at once, not when their cookie runs out).

An access rule is any object with two methods:

    admit(identity, db) -> None         at sign-in; raise AccessDenied to refuse
    still_allowed(session, db) -> bool  on every request; False ends the session

`AllowList` covers the common case. An app with its own users table (for example admin
approval) writes a small class that looks people up there.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Protocol

from .oidc import AccessDenied, Identity, parse_emails


class AccessRule(Protocol):
    def admit(self, identity: Identity, db: Any) -> None: ...

    def still_allowed(self, session: Any, db: Any) -> bool: ...


class AllowList:
    """Only these email addresses. An empty list lets nobody in (fails closed).

    `emails` may be a set, a setting string ("a@x.com, b@y.com") or a function returning
    either, so a list read from settings can change without a restart.
    """

    def __init__(self, emails: Iterable[str] | str | Callable[[], Iterable[str] | str],
                 require_mfa: bool | Callable[[], bool] = False):
        self._emails = emails
        self._mfa = require_mfa

    def emails(self) -> set[str]:
        value = self._emails() if callable(self._emails) else self._emails
        if isinstance(value, str):
            return parse_emails(value)
        return {e.strip().lower() for e in value if e and e.strip()}

    def require_mfa(self) -> bool:
        return bool(self._mfa() if callable(self._mfa) else self._mfa)

    def admit(self, identity: Identity, db: Any = None) -> None:
        if identity.email not in self.emails():
            raise AccessDenied("email_not_allowed",
                               "This account is not allowed to use this site.")
        if self.require_mfa() and not identity.mfa:
            raise AccessDenied("mfa_required", "Two-step verification is required. Set it up "
                               "with the sign-in provider and try again.")

    def still_allowed(self, session: Any, db: Any = None) -> bool:
        return (session.email or "").lower() in self.emails()
