"""Users, groups and login (local accounts and directory users)."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, Row, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.auth.acl import Viewer
from app.auth.ldap import Directory, DirectoryUser, normalise_dn
from app.auth.passwords import check_strength, hash_password, needs_rehash, verify_password
from app.core.config import Settings
from app.db.tables import groups, sessions, user_groups, users

log = logging.getLogger(__name__)

# Stored as the password hash of directory users. It isn't a valid Argon2 hash,
# so a local password check against it always fails.
DIRECTORY_PASSWORD_MARKER = "!directory"


class AccountError(ValueError):
    pass


class NotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class UserInfo:
    id: int
    username: str
    display_name: str | None
    is_admin: bool
    disabled: bool
    group_ids: list[int]
    created_at: datetime
    auth_source: str = "local"
    last_login_at: datetime | None = None


@dataclass(frozen=True)
class GroupInfo:
    id: int
    name: str
    members: int


def normalise_username(username: str) -> str:
    name = username.strip().lower()
    if not name or len(name) > 64 or any(c.isspace() for c in name):
        raise AccountError("Username must be 1-64 characters with no spaces")
    return name


# ------------------------------------------------------------------- login


def authenticate(
    engine: Engine,
    username: str,
    password: str,
    settings: Settings,
    directory: Directory | None = None,
) -> Viewer | None:
    """Returns the user on success, None otherwise, without revealing which part
    was wrong. Failures are counted per account and lock it for a few minutes.

    Local accounts always use their own password, so a local administrator can
    still sign in when the directory is down. Everyone else is checked against
    the directory (if configured). Raises DirectoryUnavailableError when the
    directory can't answer.
    """
    try:
        name = normalise_username(username)
    except AccountError:
        verify_password(None, password)
        return None
    if directory is None:
        return _authenticate_local(engine, name, password, settings)

    with engine.connect() as conn:
        row = conn.execute(select(users).where(users.c.username == name)).one_or_none()
        now = conn.execute(select(func.now())).scalar_one()
    if row is not None and row.auth_source == "local":
        return _authenticate_local(engine, name, password, settings)
    if row is not None and (row.disabled or (row.locked_until and row.locked_until > now)):
        return None  # don't even ask the directory

    found = directory.authenticate(name, password)
    if found is None:
        if row is not None:
            with engine.begin() as conn:
                _record_failure(conn, row, settings)
        return None
    required = settings.ldap_required_group
    if required and normalise_dn(required) not in found.group_dns:
        log.info("Directory user %s is not in LDAP_REQUIRED_GROUP", name)
        return None
    try:
        return _sync_directory_user(engine, settings, found)
    except IntegrityError:
        # Two first sign-ins raced to create the row; the second one syncs it.
        return _sync_directory_user(engine, settings, found)


def _record_failure(conn: Connection, row: Row[Any], settings: Settings) -> None:
    failures = row.failed_logins + 1
    values: dict[str, object] = {"failed_logins": failures}
    if failures >= settings.login_max_failures:
        values = {
            "failed_logins": 0,
            "locked_until": func.now() + timedelta(minutes=settings.login_lockout_minutes),
        }
    conn.execute(update(users).where(users.c.id == row.id).values(**values))


def _sync_directory_user(engine: Engine, settings: Settings, found: DirectoryUser) -> Viewer:
    """Create or refresh the local row for a directory user and sync the app
    groups that LDAP_GROUP_MAP manages. Other memberships are left alone."""
    managed = {normalise_dn(dn): name for dn, name in settings.ldap_group_map.items()}
    wanted_names = {managed[dn] for dn in found.group_dns if dn in managed}
    with engine.begin() as conn:
        group_id: dict[str, int] = {}
        for gname in sorted(set(managed.values())):
            conn.execute(pg_insert(groups).values(name=gname).on_conflict_do_nothing())
            group_id[gname] = conn.execute(
                select(groups.c.id).where(groups.c.name == gname)
            ).scalar_one()

        row = conn.execute(
            select(users).where(users.c.username == found.username).with_for_update()
        ).one_or_none()
        values: dict[str, object] = {
            "display_name": found.display_name,
            "ldap_dn": found.dn,
            "last_login_at": func.now(),
            "failed_logins": 0,
            "locked_until": None,
        }
        if settings.ldap_admin_group:
            values["is_admin"] = normalise_dn(settings.ldap_admin_group) in found.group_dns
        if row is None:
            user_id: int = conn.execute(
                insert(users)
                .values(
                    username=found.username,
                    password_hash=DIRECTORY_PASSWORD_MARKER,
                    auth_source="ldap",
                    **values,
                )
                .returning(users.c.id)
            ).scalar_one()
            default = conn.execute(
                select(groups.c.id).where(groups.c.name == settings.default_group)
            ).scalar_one_or_none()
            if default is not None and settings.default_group not in managed.values():
                conn.execute(insert(user_groups).values(user_id=user_id, group_id=default))
        else:
            if row.auth_source != "ldap":  # a local account with this name took precedence
                raise AccountError(f"{found.username} is a local account")
            user_id = row.id
            conn.execute(update(users).where(users.c.id == user_id).values(**values))

        managed_ids = set(group_id.values())
        wanted_ids = {group_id[n] for n in wanted_names}
        if managed_ids - wanted_ids:
            conn.execute(
                delete(user_groups).where(
                    user_groups.c.user_id == user_id,
                    user_groups.c.group_id.in_(managed_ids - wanted_ids),
                )
            )
        for gid in sorted(wanted_ids):
            conn.execute(
                pg_insert(user_groups)
                .values(user_id=user_id, group_id=gid)
                .on_conflict_do_nothing()
            )
        user = conn.execute(select(users).where(users.c.id == user_id)).one()
    return Viewer(user.id, user.username, user.is_admin, user.display_name)


def _authenticate_local(
    engine: Engine, name: str, password: str, settings: Settings
) -> Viewer | None:
    with engine.begin() as conn:
        row = conn.execute(
            select(users).where(users.c.username == name).with_for_update()
        ).one_or_none()
        locked = (
            row is not None
            and row.locked_until is not None
            and row.locked_until > conn.execute(select(func.now())).scalar_one()
        )
        ok = verify_password(row.password_hash if row else None, password)
        if row is None:
            return None
        if locked or row.disabled or not ok:
            if not ok:
                _record_failure(conn, row, settings)
            return None
        values: dict[str, object] = {
            "failed_logins": 0,
            "locked_until": None,
            "last_login_at": func.now(),
        }
        if needs_rehash(row.password_hash):
            values["password_hash"] = hash_password(password)
        conn.execute(update(users).where(users.c.id == row.id).values(**values))
        return Viewer(row.id, row.username, row.is_admin, row.display_name)


# ------------------------------------------------------------------- users


def _group_ids(engine: Engine, user_id: int) -> list[int]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(user_groups.c.group_id)
                .where(user_groups.c.user_id == user_id)
                .order_by(user_groups.c.group_id)
            ).scalars()
        )


def create_user(
    engine: Engine,
    settings: Settings,
    *,
    username: str,
    password: str,
    is_admin: bool = False,
    display_name: str | None = None,
    group_ids: list[int] | None = None,
) -> int:
    """New users join the default group unless group_ids is given."""
    name = normalise_username(username)
    check_strength(password, settings.password_min_length)
    try:
        with engine.begin() as conn:
            user_id: int = conn.execute(
                insert(users)
                .values(
                    username=name,
                    display_name=display_name,
                    password_hash=hash_password(password),
                    is_admin=is_admin,
                )
                .returning(users.c.id)
            ).scalar_one()
            if group_ids is None:
                default = conn.execute(
                    select(groups.c.id).where(groups.c.name == settings.default_group)
                ).scalar_one_or_none()
                group_ids = [default] if default is not None else []
            if group_ids:
                conn.execute(
                    insert(user_groups), [{"user_id": user_id, "group_id": g} for g in group_ids]
                )
    except IntegrityError as exc:
        raise AccountError(f"User '{name}' already exists or a group doesn't exist") from exc
    return user_id


def find_user_id(engine: Engine, username: str) -> int | None:
    with engine.connect() as conn:
        found: int | None = conn.execute(
            select(users.c.id).where(users.c.username == normalise_username(username))
        ).scalar_one_or_none()
    return found


def set_password(
    engine: Engine, settings: Settings, user_id: int, password: str, *, keep_session: str | None
) -> None:
    """Changes a local account's password and ends the user's other sessions."""
    check_strength(password, settings.password_min_length)
    with engine.begin() as conn:
        source = conn.execute(
            select(users.c.auth_source).where(users.c.id == user_id)
        ).scalar_one_or_none()
        if source is None:
            raise NotFoundError(user_id)
        if source != "local":
            raise AccountError(
                "This account signs in with the company directory; change the password there"
            )
        conn.execute(
            update(users)
            .where(users.c.id == user_id)
            .values(password_hash=hash_password(password), failed_logins=0, locked_until=None)
        )
        stmt = delete(sessions).where(sessions.c.user_id == user_id)
        if keep_session:
            stmt = stmt.where(sessions.c.token_hash != keep_session)
        conn.execute(stmt)


def list_users(engine: Engine) -> list[UserInfo]:
    with engine.connect() as conn:
        rows = conn.execute(select(users).order_by(users.c.username)).all()
        memberships = conn.execute(select(user_groups)).all()
    by_user: dict[int, list[int]] = {}
    for m in memberships:
        by_user.setdefault(m.user_id, []).append(m.group_id)
    return [
        UserInfo(
            r.id,
            r.username,
            r.display_name,
            r.is_admin,
            r.disabled,
            sorted(by_user.get(r.id, [])),
            r.created_at,
            r.auth_source,
            r.last_login_at,
        )
        for r in rows
    ]


def get_user(engine: Engine, user_id: int) -> UserInfo:
    for u in list_users(engine):
        if u.id == user_id:
            return u
    raise NotFoundError(user_id)


def update_user(
    engine: Engine,
    user_id: int,
    *,
    acting_user_id: int,
    display_name: str | None = None,
    is_admin: bool | None = None,
    disabled: bool | None = None,
    group_ids: list[int] | None = None,
) -> None:
    if user_id == acting_user_id and (is_admin is False or disabled is True):
        raise AccountError("You can't remove your own admin rights or disable yourself")
    values: dict[str, object] = {}
    if display_name is not None:
        values["display_name"] = display_name or None
    if is_admin is not None:
        values["is_admin"] = is_admin
    if disabled is not None:
        values["disabled"] = disabled
    try:
        with engine.begin() as conn:
            exists = conn.execute(select(users.c.id).where(users.c.id == user_id)).scalar()
            if exists is None:
                raise NotFoundError(user_id)
            if values:
                conn.execute(update(users).where(users.c.id == user_id).values(**values))
            if disabled:
                conn.execute(delete(sessions).where(sessions.c.user_id == user_id))
            if group_ids is not None:
                conn.execute(delete(user_groups).where(user_groups.c.user_id == user_id))
                if group_ids:
                    conn.execute(
                        insert(user_groups),
                        [{"user_id": user_id, "group_id": g} for g in sorted(set(group_ids))],
                    )
    except IntegrityError as exc:
        raise AccountError("One of those groups doesn't exist") from exc


def delete_user(engine: Engine, user_id: int, *, acting_user_id: int) -> None:
    """Sessions and memberships cascade; their uploads stay (uploaded_by -> NULL)."""
    if user_id == acting_user_id:
        raise AccountError("You can't delete your own account")
    with engine.begin() as conn:
        if conn.execute(delete(users).where(users.c.id == user_id)).rowcount == 0:
            raise NotFoundError(user_id)


def user_group_ids(engine: Engine, user_id: int) -> list[int]:
    return _group_ids(engine, user_id)


# ------------------------------------------------------------------ groups


def list_groups(engine: Engine) -> list[GroupInfo]:
    members = (
        select(func.count())
        .where(user_groups.c.group_id == groups.c.id)
        .correlate(groups)
        .scalar_subquery()
    )
    with engine.connect() as conn:
        rows = conn.execute(
            select(groups.c.id, groups.c.name, members.label("members")).order_by(groups.c.name)
        ).all()
    return [GroupInfo(r.id, r.name, r.members) for r in rows]


def create_group(engine: Engine, name: str) -> int:
    clean = " ".join(name.split())
    if not clean or len(clean) > 100:
        raise AccountError("Group name must be 1-100 characters")
    try:
        with engine.begin() as conn:
            group_id: int = conn.execute(
                insert(groups).values(name=clean).returning(groups.c.id)
            ).scalar_one()
    except IntegrityError as exc:
        raise AccountError(f"Group '{clean}' already exists") from exc
    return group_id


def delete_group(engine: Engine, group_id: int) -> None:
    with engine.begin() as conn:
        if conn.execute(delete(groups).where(groups.c.id == group_id)).rowcount == 0:
            raise NotFoundError(group_id)


def group_ids_by_name(engine: Engine, names: list[str]) -> list[int]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(groups.c.id, groups.c.name).where(groups.c.name.in_(names))
        ).all()
    found = {r.name: r.id for r in rows}
    missing = [n for n in names if n not in found]
    if missing:
        raise AccountError(f"Unknown group(s): {', '.join(missing)}")
    return [found[n] for n in names]
