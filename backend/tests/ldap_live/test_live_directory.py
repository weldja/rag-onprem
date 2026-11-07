"""Directory sign-in against a real OpenLDAP server over TLS.
Run via scripts/test-ldap.sh, which starts the server and seeds it."""

import os

import pytest

from app.auth.ldap import DirectoryUnavailableError, LdapDirectory, normalise_dn
from app.core.config import Settings

pytestmark = [pytest.mark.ldap, pytest.mark.enable_socket]

HOST = os.environ.get("LDAP_TEST_HOST", "ldap.test")
CA = os.environ.get("LDAP_TEST_CA", "/ldap-ca/ca.crt")
BASE = "dc=example,dc=test"


def live(**overrides: object) -> LdapDirectory:
    values: dict[str, object] = {
        "ldap_enabled": True,
        "ldap_url": f"ldaps://{HOST}:636",
        "ldap_ca_file": CA,
        "ldap_bind_dn": f"cn=admin,{BASE}",
        "ldap_bind_password": "test-admin-password",
        "ldap_user_base_dn": f"ou=people,{BASE}",
        "ldap_user_filter": "(&(objectClass=inetOrgPerson)(uid={username}))",
        "ldap_display_name_attr": "cn",
        "ldap_group_base_dn": f"ou=groups,{BASE}",
        "ldap_group_filter": "(&(objectClass=groupOfNames)(member={user_dn}))",
        "ldap_timeout_s": 5,
    }
    values.update(overrides)
    return LdapDirectory(Settings(**values))  # type: ignore[arg-type]


def test_ldaps_sign_in_with_groups() -> None:
    user = live().authenticate("ann", "ann-directory-pw")
    assert user is not None
    assert user.dn == f"uid=ann,ou=people,{BASE}"
    assert user.display_name == "Ann Smith"
    assert user.group_dns == {normalise_dn(f"cn=finance,ou=groups,{BASE}")}


def test_starttls_sign_in() -> None:
    d = live(ldap_url=f"ldap://{HOST}:389", ldap_start_tls=True)
    assert d.authenticate("ann", "ann-directory-pw") is not None


def test_wrong_password_and_unknown_user() -> None:
    d = live()
    assert d.authenticate("ann", "not-her-password") is None
    assert d.authenticate("nobody", "whatever-password") is None


def test_untrusted_certificate_is_refused() -> None:
    # Without the directory's CA the certificate can't be verified: no sign-in,
    # and certainly no password sent over an unverified connection.
    with pytest.raises(DirectoryUnavailableError):
        live(ldap_ca_file="").authenticate("ann", "ann-directory-pw")


def test_wrong_service_password_is_reported_as_unavailable() -> None:
    with pytest.raises(DirectoryUnavailableError):
        live(ldap_bind_password="rotated").authenticate("ann", "ann-directory-pw")
