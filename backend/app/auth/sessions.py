"""Server-side sessions in Postgres. The browser holds a random token; only its
SHA-256 is stored, so a database leak doesn't hand out live sessions."""

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import Engine, delete, func, insert, select, update

from app.auth.acl import Viewer
from app.core.config import Settings
from app.db.tables import sessions, users

COOKIE_NAME = "rag_session"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(engine: Engine, user_id: int, settings: Settings) -> str:
    token = secrets.token_urlsafe(32)
    with engine.begin() as conn:
        conn.execute(
            insert(sessions).values(
                token_hash=token_hash(token),
                user_id=user_id,
                expires_at=func.now() + timedelta(hours=settings.session_idle_hours),
            )
        )
    return token


def resolve(engine: Engine, token: str, settings: Settings) -> Viewer | None:
    """The session's user, sliding the idle expiry forward (capped at the
    absolute maximum). None if missing, expired, or the user is disabled."""
    hashed = token_hash(token)
    with engine.begin() as conn:
        row = conn.execute(
            select(
                users.c.id,
                users.c.username,
                users.c.is_admin,
                users.c.display_name,
                sessions.c.created_at,
            )
            .join(users, users.c.id == sessions.c.user_id)
            .where(
                sessions.c.token_hash == hashed,
                sessions.c.expires_at > func.now(),
                sessions.c.created_at > func.now() - timedelta(days=settings.session_max_days),
                users.c.disabled.is_(False),
            )
        ).one_or_none()
        if row is None:
            return None
        conn.execute(
            update(sessions)
            .where(sessions.c.token_hash == hashed)
            .values(
                last_seen_at=func.now(),
                expires_at=func.now() + timedelta(hours=settings.session_idle_hours),
            )
        )
    return Viewer(row.id, row.username, row.is_admin, row.display_name)


def end_session(engine: Engine, token: str) -> None:
    with engine.begin() as conn:
        conn.execute(delete(sessions).where(sessions.c.token_hash == token_hash(token)))


def purge_expired(engine: Engine) -> int:
    with engine.begin() as conn:
        return conn.execute(delete(sessions).where(sessions.c.expires_at <= func.now())).rowcount
