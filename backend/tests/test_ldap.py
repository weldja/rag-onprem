"""Directory sign-in logic against ldap3's in-memory mock server (no network)."""

import pytest
from ldap3 import MOCK_SYNC, Connection, Server

from app.auth.ldap import (
    DirectoryConfigError,
    DirectoryUnavailableError,
    LdapDirectory,
    normalise_dn,
)
from app.core.config import Settings

BASE = "DC=example,DC=local"
STAFF = f"OU=Staff,{BASE}"
GROUPS = f"OU=Groups,{BASE}"


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "ldap_enabled": True,
        "ldap_url": "ldaps://dc1.example.local",
        "ldap_bind_dn": f"CN=svc-rag,{BASE}",
        "ldap_bind_password": "service-secret",
        "ldap_user_base_dn": STAFF,
        "ldap_group_base_dn": GROUPS,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture
def server() -> Server:
    srv = Server("mock-ad")
    seed = Connection(srv, user="x", password="x", client_strategy=MOCK_SYNC)
    add = seed.strategy.add_entry
    add(f"CN=svc-rag,{BASE}", {"objectClass": "user", "userPassword": "service-secret"})
    add(
        f"CN=Ann Smith,{STAFF}",
        {
            "objectClass": "user",
            "sAMAccountName": "ann",
            "displayName": "Ann Smith",
            "userPassword": "ann-password-1",
        },
    )
    add(
        f"CN=Bob (contractor)*,{STAFF}",
        {"objectClass": "user", "sAMAccountName": "bob", "userPassword": "bob-password-1"},
    )
    add(
        f"CN=Finance,{GROUPS}",
        {"objectClass": "group", "member": [f"CN=Ann Smith,{STAFF}"]},
    )
    add(f"CN=HR,{GROUPS}", {"objectClass": "group", "member": [f"CN=Bob (contractor)*,{STAFF}"]})
    return srv


def mock_conn(srv: Server, user: str, password: str) -> Connection:
    return Connection(srv, user=user, password=password, client_strategy=MOCK_SYNC)


def directory(server: Server, **overrides: object) -> LdapDirectory:
    return LdapDirectory(settings(**overrides), server=server, connection_factory=mock_conn)


def test_valid_credentials_return_user_with_groups(server: Server) -> None:
    user = directory(server).authenticate("ann", "ann-password-1")
    assert user is not None
    assert user.dn == f"CN=Ann Smith,{STAFF}"
    assert user.display_name == "Ann Smith"
    assert user.group_dns == {normalise_dn(f"CN=Finance,{GROUPS}")}


def test_wrong_password_and_unknown_user_are_rejected(server: Server) -> None:
    d = directory(server)
    assert d.authenticate("ann", "wrong") is None
    assert d.authenticate("nobody", "whatever") is None


@pytest.mark.parametrize("password", ["", None])
def test_empty_password_never_reaches_a_bind(server: Server, password: str | None) -> None:
    calls: list[str] = []

    def spy(srv: Server, user: str, pw: str) -> Connection:
        calls.append(user)
        return mock_conn(srv, user, pw)

    d = LdapDirectory(settings(), server=server, connection_factory=spy)
    assert d.authenticate("ann", password) is None  # type: ignore[arg-type]
    assert calls == []


def test_filter_injection_is_escaped(server: Server) -> None:
    # Without escaping, "*" would match every account and ")(" could rewrite the filter.
    d = directory(server)
    assert d.authenticate("*", "ann-password-1") is None
    assert d.authenticate("ann)(sAMAccountName=*", "ann-password-1") is None


def test_dn_with_special_characters_is_escaped_in_group_search(server: Server) -> None:
    user = directory(server).authenticate("bob", "bob-password-1")
    assert user is not None
    assert user.group_dns == {normalise_dn(f"CN=HR,{GROUPS}")}


def test_service_account_failure_is_unavailable_not_a_wrong_password(server: Server) -> None:
    d = directory(server, ldap_bind_password="rotated")
    with pytest.raises(DirectoryUnavailableError):
        d.authenticate("ann", "ann-password-1")


def test_no_group_base_means_no_groups(server: Server) -> None:
    user = directory(server, ldap_group_base_dn="").authenticate("ann", "ann-password-1")
    assert user is not None and user.group_dns == frozenset()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"ldap_url": ""}, "LDAP_URL"),
        ({"ldap_user_base_dn": ""}, "LDAP_USER_BASE_DN"),
        ({"ldap_user_filter": "(uid=x)"}, "{username}"),
        ({"ldap_url": "ldap://dc1.example.local"}, "unencrypted"),
    ],
)
def test_bad_configuration_fails_at_start_up(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(DirectoryConfigError, match=message.replace("{", r"\{").replace("}", r"\}")):
        LdapDirectory(settings(**overrides))


def test_plaintext_only_when_explicitly_allowed_or_starttls() -> None:
    LdapDirectory(settings(ldap_url="ldap://dc1", ldap_start_tls=True))
    LdapDirectory(settings(ldap_url="ldap://dc1", ldap_allow_plaintext=True))


def test_normalise_dn() -> None:
    assert normalise_dn("CN=Finance, OU=Groups ,DC=Example") == "cn=finance,ou=groups,dc=example"


def test_group_map_parses_from_environment_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LDAP_GROUP_MAP", '{"CN=Finance,OU=Groups,DC=example,DC=local": "Finance"}')
    assert Settings().ldap_group_map == {"CN=Finance,OU=Groups,DC=example,DC=local": "Finance"}
