"""Sign in/out, current user, own password, and user/group administration."""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.api.deps import AdminUser, AppSettings, CurrentUser, Db
from app.auth import accounts, sessions
from app.auth.passwords import WeakPasswordError, verify_password
from app.db.tables import users

router = APIRouter(prefix="/api", tags=["auth"])


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class GroupRef(BaseModel):
    id: int
    name: str


class MeOut(BaseModel):
    id: int
    username: str
    display_name: str | None
    is_admin: bool
    groups: list[GroupRef]


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str | None
    is_admin: bool
    disabled: bool
    group_ids: list[int]
    created_at: datetime


class UserCreateIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(max_length=1024)
    display_name: str | None = Field(default=None, max_length=200)
    is_admin: bool = False
    group_ids: list[int] | None = None


class UserUpdateIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    is_admin: bool | None = None
    disabled: bool | None = None
    group_ids: list[int] | None = None
    password: str | None = Field(default=None, max_length=1024)


class GroupOut(BaseModel):
    id: int
    name: str
    members: int


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


def _me(engine: Db, viewer_id: int) -> MeOut:
    user = accounts.get_user(engine, viewer_id)
    names = {g.id: g.name for g in accounts.list_groups(engine)}
    return MeOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        is_admin=user.is_admin,
        groups=[GroupRef(id=g, name=names[g]) for g in user.group_ids if g in names],
    )


# ------------------------------------------------------------------ session


@router.post("/auth/login", response_model=MeOut)
def login(body: LoginIn, response: Response, engine: Db, settings: AppSettings) -> MeOut:
    viewer = accounts.authenticate(engine, body.username, body.password, settings)
    if viewer is None:
        # One message for every failure, so it doesn't reveal which accounts exist.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect username or password")
    token = sessions.create_session(engine, viewer.id, settings)
    response.set_cookie(
        sessions.COOKIE_NAME,
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
        max_age=int(settings.session_max_days * 86400),
    )
    return _me(engine, viewer.id)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, engine: Db) -> Response:
    token = request.cookies.get(sessions.COOKIE_NAME)
    if token:
        sessions.end_session(engine, token)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(sessions.COOKIE_NAME, path="/")
    return response


@router.get("/auth/me", response_model=MeOut)
def me(viewer: CurrentUser, engine: Db) -> MeOut:
    return _me(engine, viewer.id)


@router.post("/auth/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    body: PasswordIn, request: Request, viewer: CurrentUser, engine: Db, settings: AppSettings
) -> Response:
    with engine.connect() as conn:
        current_hash = conn.execute(
            users.select().with_only_columns(users.c.password_hash).where(users.c.id == viewer.id)
        ).scalar_one()
    if not verify_password(current_hash, body.current_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect")
    token = request.cookies.get(sessions.COOKIE_NAME)
    try:
        accounts.set_password(
            engine,
            settings,
            viewer.id,
            body.new_password,
            keep_session=sessions.token_hash(token) if token else None,
        )
    except WeakPasswordError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/groups", response_model=list[GroupOut])
def groups_for_sharing(_: CurrentUser, engine: Db) -> list[GroupOut]:
    """All groups (names only matter for the share picker)."""
    return [GroupOut(id=g.id, name=g.name, members=g.members) for g in accounts.list_groups(engine)]


# -------------------------------------------------------------------- admin


def _user_out(u: accounts.UserInfo) -> UserOut:
    return UserOut(**u.__dict__)


@router.get("/admin/users", response_model=list[UserOut])
def list_users(_: AdminUser, engine: Db) -> list[UserOut]:
    return [_user_out(u) for u in accounts.list_users(engine)]


@router.post("/admin/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreateIn, _: AdminUser, engine: Db, settings: AppSettings) -> UserOut:
    try:
        user_id = accounts.create_user(
            engine,
            settings,
            username=body.username,
            password=body.password,
            is_admin=body.is_admin,
            display_name=body.display_name,
            group_ids=body.group_ids,
        )
    except (accounts.AccountError, WeakPasswordError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return _user_out(accounts.get_user(engine, user_id))


@router.patch("/admin/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: int, body: UserUpdateIn, admin: AdminUser, engine: Db, settings: AppSettings
) -> UserOut:
    try:
        accounts.update_user(
            engine,
            user_id,
            acting_user_id=admin.id,
            display_name=body.display_name,
            is_admin=body.is_admin,
            disabled=body.disabled,
            group_ids=body.group_ids,
        )
        if body.password is not None:
            accounts.set_password(engine, settings, user_id, body.password, keep_session=None)
    except accounts.NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found") from exc
    except (accounts.AccountError, WeakPasswordError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return _user_out(accounts.get_user(engine, user_id))


@router.delete("/admin/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int, admin: AdminUser, engine: Db) -> Response:
    try:
        accounts.delete_user(engine, user_id, acting_user_id=admin.id)
    except accounts.NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found") from exc
    except accounts.AccountError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/admin/groups", response_model=list[GroupOut])
def list_groups(_: AdminUser, engine: Db) -> list[GroupOut]:
    return [GroupOut(id=g.id, name=g.name, members=g.members) for g in accounts.list_groups(engine)]


@router.post("/admin/groups", response_model=GroupOut, status_code=status.HTTP_201_CREATED)
def create_group(body: GroupIn, _: AdminUser, engine: Db) -> GroupOut:
    try:
        group_id = accounts.create_group(engine, body.name)
    except accounts.AccountError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return next(
        GroupOut(id=g.id, name=g.name, members=g.members)
        for g in accounts.list_groups(engine)
        if g.id == group_id
    )


@router.delete("/admin/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_group(group_id: int, _: AdminUser, engine: Db) -> Response:
    try:
        accounts.delete_group(engine, group_id)
    except accounts.NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
