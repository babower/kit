"""Server-side sessions and pending sign-in attempts, stored with SQLAlchemy.

The browser holds only a random token; the database keeps its SHA-256 hash, so a database
leak cannot be used to sign in, and signing out (or revoking everyone) really ends a
session. Sessions end after an idle time and after an absolute lifetime, whichever comes
first. A pending sign-in (`state`) is single use and short-lived.

The tables are defined on the app's own declarative Base, with names the app chooses, so
they live in the app's migrations (Alembic) or `create_all`, on Postgres or SQLite.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import BigInteger, DateTime, Integer, String, delete, func, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

# BIGINT keys on Postgres; SQLite only auto-numbers a plain INTEGER primary key.
Id = BigInteger().with_variant(Integer(), "sqlite")
TOUCH_INTERVAL = timedelta(minutes=1)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; everything here is UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Tables:
    Session: Any  # the sessions model class
    State: Any  # the pending sign-ins model class


def define_tables(base: Any, sessions: str = "signin_sessions",
                  states: str = "signin_states") -> Tables:
    """Add the two tables to `base` (the app's DeclarativeBase) and return their classes.
    Call it once, at import time, next to the app's other models."""

    class SessionRow(base):
        """A signed-in browser. `csrf_token` is the per-session secret every form echoes."""

        __tablename__ = sessions

        id: Mapped[int] = mapped_column(Id, primary_key=True)
        token_hash: Mapped[str] = mapped_column(String(64), unique=True)
        csrf_token: Mapped[str] = mapped_column(String(64))
        created_at: Mapped[datetime] = mapped_column(
            DateTime(timezone=True), server_default=func.now())
        last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
        expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
        revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                            nullable=True)
        ip: Mapped[str] = mapped_column(String(64), default="")
        user_agent: Mapped[str] = mapped_column(String(200), default="")
        # How they signed in ("oidc", or an app's own method such as "password") and who.
        provider: Mapped[str] = mapped_column(String(16), default="oidc")
        subject: Mapped[str] = mapped_column(String(255), default="")
        email: Mapped[str] = mapped_column(String(320), default="")
        name: Mapped[str] = mapped_column(String(200), default="")

    class StateRow(base):
        """One pending sign-in. Only the state's hash is stored; the browser holds the value
        in a cookie. Deleted when used, so a state works exactly once."""

        __tablename__ = states

        id: Mapped[int] = mapped_column(Id, primary_key=True)
        state_hash: Mapped[str] = mapped_column(String(64), unique=True)
        nonce: Mapped[str] = mapped_column(String(64))
        code_verifier: Mapped[str] = mapped_column(String(128))
        next_path: Mapped[str] = mapped_column(String(500), default="/")
        created_at: Mapped[datetime] = mapped_column(
            DateTime(timezone=True), server_default=func.now())
        expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    SessionRow.__name__ = SessionRow.__qualname__ = f"SignInSession_{sessions}"
    StateRow.__name__ = StateRow.__qualname__ = f"SignInState_{states}"
    return Tables(SessionRow, StateRow)


class Store:
    """Session and state operations on a set of tables."""

    def __init__(self, tables: Tables, idle: timedelta, max_age: timedelta):
        self.t = tables
        self.idle = idle
        self.max_age = max_age

    # --- sessions -------------------------------------------------------------------------
    def create(self, db: Session, ip: str = "", user_agent: str = "", *, provider: str = "oidc",
               subject: str = "", email: str = "", name: str = "") -> tuple[Any, str]:
        """A fresh session (never an old one reused, so a session cannot be fixated).
        Returns (row, token); the token goes in the cookie and nowhere else."""
        S = self.t.Session
        now = datetime.now(UTC)
        db.execute(delete(S).where(S.expires_at < now - timedelta(days=1)))
        token = secrets.token_urlsafe(32)
        row = S(token_hash=digest(token), csrf_token=secrets.token_urlsafe(24),
                last_seen_at=now, expires_at=now + self.max_age, ip=ip[:64],
                user_agent=user_agent[:200], provider=provider[:16], subject=subject[:255],
                email=email[:320], name=name[:200])
        db.add(row)
        db.commit()
        return row, token

    def lookup(self, db: Session, token: str | None) -> Any | None:
        """The live session for this cookie value, or None (unknown, revoked, idle, expired)."""
        if not token:
            return None
        S = self.t.Session
        row = db.scalars(select(S).where(S.token_hash == digest(token))).first()
        if row is None or row.revoked_at is not None:
            return None
        now = datetime.now(UTC)
        if now >= aware(row.expires_at) or now - aware(row.last_seen_at) > self.idle:
            return None
        if now - aware(row.last_seen_at) > TOUCH_INTERVAL:
            row.last_seen_at = now
            db.commit()
        return row

    def revoke(self, db: Session, row: Any) -> None:
        row.revoked_at = datetime.now(UTC)
        db.commit()

    def revoke_all(self, db: Session, *, email: str | None = None) -> int:
        """End every live session, or every session of one person."""
        S = self.t.Session
        q = update(S).where(S.revoked_at.is_(None))
        if email is not None:
            q = q.where(S.email == email.strip().lower())
        result = db.execute(q.values(revoked_at=datetime.now(UTC)))
        db.commit()
        return result.rowcount or 0

    # --- pending sign-ins -------------------------------------------------------------------
    def save_state(self, db: Session, state: str, nonce: str, verifier: str, next_path: str,
                   ttl: timedelta) -> None:
        T = self.t.State
        now = datetime.now(UTC)
        db.execute(delete(T).where(T.expires_at < now))  # tidy up abandoned attempts
        db.add(T(state_hash=digest(state), nonce=nonce, code_verifier=verifier,
                 next_path=next_path, expires_at=now + ttl))
        db.commit()

    def take_state(self, db: Session, state: str) -> tuple[str, str, str, bool] | None:
        """Remove a pending sign-in and return (nonce, verifier, next_path, expired), or
        None if it is unknown or already used. It is deleted whatever happens next."""
        T = self.t.State
        row = db.scalars(select(T).where(T.state_hash == digest(state))).first()
        if row is None:
            return None
        out = (row.nonce, row.code_verifier, row.next_path,
               aware(row.expires_at) < datetime.now(UTC))
        db.delete(row)
        db.commit()
        return out
