"""Users, groups and login."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.auth.acl import Viewer
from app.auth.passwords import check_strength, hash_password, needs_rehash, verify_password
from app.core.config import Settings
from app.db.tables import groups, sessions, user_groups, users


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


def authenticate(engine: Engine, username: str, password: str, settings: Settings) -> Viewer | None:
    """Returns the user on success. Counts failures and locks the account for a
    few minutes after too many, without revealing which part was wrong."""
    try:
        name = normalise_username(username)
    except AccountError:
        verify_password(None, password)
        return None
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
                failures = row.failed_logins + 1
                values: dict[str, object] = {"failed_logins": failures}
                if failures >= settings.login_max_failures:
                    values = {
                        "failed_logins": 0,
                        "locked_until": func.now()
                        + timedelta(minutes=settings.login_lockout_minutes),
                    }
                conn.execute(update(users).where(users.c.id == row.id).values(**values))
            return None
        values = {"failed_logins": 0, "locked_until": None}
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
    """Changes the password and ends the user's other sessions."""
    check_strength(password, settings.password_min_length)
    with engine.begin() as conn:
        result = conn.execute(
            update(users)
            .where(users.c.id == user_id)
            .values(password_hash=hash_password(password), failed_logins=0, locked_until=None)
        )
        if result.rowcount == 0:
            raise NotFoundError(user_id)
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
