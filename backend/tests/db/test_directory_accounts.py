"""Directory users in the database: creation, group sync, admin mapping, and
local accounts taking precedence. Uses a fake directory, a real Postgres."""

import pytest
from sqlalchemy import Engine

from app.auth import accounts, sessions
from app.auth.ldap import DirectoryUnavailableError, DirectoryUser, normalise_dn
from app.core.config import Settings

pytestmark = [pytest.mark.db, pytest.mark.enable_socket]

FINANCE = "CN=Finance,OU=Groups,DC=ex"
HR = "CN=HR,OU=Groups,DC=ex"
ADMINS = "CN=RAG Admins,OU=Groups,DC=ex"
STAFF = "CN=All Employees,OU=Groups,DC=ex"


class FakeDirectory:
    def __init__(self) -> None:
        self.users: dict[str, tuple[str, set[str]]] = {}  # name -> (password, group DNs)
        self.calls = 0
        self.down = False

    def authenticate(self, username: str, password: str) -> DirectoryUser | None:
        self.calls += 1
        if self.down:
            raise DirectoryUnavailableError("down")
        entry = self.users.get(username)
        if entry is None or entry[0] != password:
            return None
        return DirectoryUser(
            username,
            f"CN={username},OU=Staff,DC=ex",
            username.title(),
            frozenset(normalise_dn(g) for g in entry[1]),
        )


def ldap_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "ldap_group_map": {FINANCE: "Finance", HR: "HR"},
        "ldap_admin_group": ADMINS,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def groups_of(db: Engine, user_id: int) -> set[str]:
    names = {g.id: g.name for g in accounts.list_groups(db)}
    return {names[g] for g in accounts.get_user(db, user_id).group_ids}


def test_first_sign_in_creates_a_directory_user(db: Engine) -> None:
    d = FakeDirectory()
    d.users["ann"] = ("pw-ann", {FINANCE})
    viewer = accounts.authenticate(db, "Ann", "pw-ann", ldap_settings(), d)
    assert viewer is not None and viewer.username == "ann" and not viewer.is_admin
    user = accounts.get_user(db, viewer.id)
    assert user.auth_source == "ldap" and user.display_name == "Ann"
    assert user.last_login_at is not None
    # Mapped group created and joined; default group joined as for local users.
    assert groups_of(db, viewer.id) == {"Finance", "All staff"}


def test_group_sync_touches_only_mapped_groups(db: Engine) -> None:
    d = FakeDirectory()
    d.users["ann"] = ("pw-ann", {FINANCE})
    s = ldap_settings()
    ann = accounts.authenticate(db, "ann", "pw-ann", s, d)
    assert ann is not None
    projects = accounts.create_group(db, "Projects")  # managed by hand in the app
    accounts.update_user(
        db, ann.id, acting_user_id=-1, group_ids=accounts.user_group_ids(db, ann.id) + [projects]
    )
    d.users["ann"] = ("pw-ann", {HR})  # moved department in AD
    assert accounts.authenticate(db, "ann", "pw-ann", s, d) is not None
    assert groups_of(db, ann.id) == {"HR", "All staff", "Projects"}


def test_admin_rights_follow_the_admin_group(db: Engine) -> None:
    d = FakeDirectory()
    d.users["root"] = ("pw", {ADMINS})
    s = ldap_settings()
    viewer = accounts.authenticate(db, "root", "pw", s, d)
    assert viewer is not None and viewer.is_admin
    d.users["root"] = ("pw", set())
    viewer = accounts.authenticate(db, "root", "pw", s, d)
    assert viewer is not None and not viewer.is_admin


def test_required_group_restricts_sign_in(db: Engine) -> None:
    d = FakeDirectory()
    d.users["eve"] = ("pw", {HR})
    s = ldap_settings(ldap_required_group=STAFF)
    assert accounts.authenticate(db, "eve", "pw", s, d) is None
    d.users["eve"] = ("pw", {HR, STAFF})
    assert accounts.authenticate(db, "eve", "pw", s, d) is not None


def test_local_accounts_win_and_work_when_the_directory_is_down(db: Engine) -> None:
    s = ldap_settings()
    accounts.create_user(
        db, s, username="breakglass", password="a-long-local-password", is_admin=True
    )
    d = FakeDirectory()
    d.users["breakglass"] = ("directory-pw", {ADMINS})
    d.down = True
    viewer = accounts.authenticate(db, "breakglass", "a-long-local-password", s, d)
    assert viewer is not None and viewer.is_admin
    assert accounts.authenticate(db, "breakglass", "directory-pw", s, d) is None
    assert d.calls == 0  # local accounts never consult the directory


def test_directory_outage_is_reported_not_treated_as_wrong_password(db: Engine) -> None:
    d = FakeDirectory()
    d.down = True
    with pytest.raises(DirectoryUnavailableError):
        accounts.authenticate(db, "ann", "pw", ldap_settings(), d)


def test_disabled_and_locked_directory_users_are_refused_without_asking(db: Engine) -> None:
    d = FakeDirectory()
    d.users["ann"] = ("pw-ann", set())
    s = ldap_settings(login_max_failures=2)
    ann = accounts.authenticate(db, "ann", "pw-ann", s, d)
    assert ann is not None
    for _ in range(2):
        assert accounts.authenticate(db, "ann", "wrong", s, d) is None
    calls = d.calls
    assert accounts.authenticate(db, "ann", "pw-ann", s, d) is None  # locked
    assert d.calls == calls

    accounts.update_user(db, ann.id, acting_user_id=-1, disabled=True)
    token = sessions.create_session(db, ann.id, s)
    assert sessions.resolve(db, token, s) is None


def test_directory_passwords_cannot_be_set_or_used_locally(db: Engine) -> None:
    d = FakeDirectory()
    d.users["ann"] = ("pw-ann", set())
    s = ldap_settings()
    ann = accounts.authenticate(db, "ann", "pw-ann", s, d)
    assert ann is not None
    with pytest.raises(accounts.AccountError, match="directory"):
        accounts.set_password(db, s, ann.id, "a-new-local-password", keep_session=None)
    # With directory sign-in switched off, the account can't sign in at all.
    assert accounts.authenticate(db, "ann", "pw-ann", s, None) is None
    assert accounts.authenticate(db, "ann", accounts.DIRECTORY_PASSWORD_MARKER, s, None) is None
