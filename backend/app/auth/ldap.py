"""Directory sign-in: LDAP / Active Directory, search-then-bind.

1. Bind as the service account and find exactly one user entry for the
   sign-in name (escaped into LDAP_USER_FILTER).
2. Bind as that entry's DN with the supplied password. This is the actual check.
3. Optionally search LDAP_GROUP_BASE_DN for the user's groups.

Security rules enforced here:
- Empty passwords are refused before any bind. LDAP treats a simple bind with
  a DN and an empty password as an *anonymous* bind and reports success.
- User input is escaped before it goes into a filter.
- TLS (ldaps:// or StartTLS) with certificate verification is required unless
  LDAP_ALLOW_PLAINTEXT is set (isolated test setups only).

Uses ldap3 (pure Python, LGPL-3.0, used unmodified).
"""

import contextlib
import logging
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ldap3 import NONE, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

from app.core.config import Settings

log = logging.getLogger(__name__)


class DirectoryUnavailableError(RuntimeError):
    """The directory couldn't be reached or the service account was refused."""


class DirectoryConfigError(ValueError):
    pass


@dataclass(frozen=True)
class DirectoryUser:
    username: str
    dn: str
    display_name: str | None
    group_dns: frozenset[str]  # normalised with normalise_dn()


class Directory(Protocol):
    def authenticate(self, username: str, password: str) -> DirectoryUser | None:
        """The user if the password is right; None if not (or unknown user).
        Raises DirectoryUnavailableError if the directory can't answer."""
        ...


def normalise_dn(dn: str) -> str:
    """Case- and whitespace-insensitive form for comparing DNs from config with
    DNs from the directory ("CN=Finance, OU=Groups" == "cn=finance,ou=groups")."""
    return ",".join("=".join(p.strip() for p in rdn.split("=", 1)) for rdn in dn.split(",")).lower()


ConnectionFactory = Callable[[Server, str, str], Connection]


def _default_connection(server: Server, user: str, password: str) -> Connection:
    return Connection(server, user=user, password=password, read_only=True, raise_exceptions=False)


class LdapDirectory:
    def __init__(
        self,
        settings: Settings,
        *,
        server: Server | None = None,
        connection_factory: ConnectionFactory = _default_connection,
    ) -> None:
        s = settings
        if not s.ldap_url:
            raise DirectoryConfigError("LDAP_ENABLED is true but LDAP_URL is empty")
        if not s.ldap_user_base_dn:
            raise DirectoryConfigError("LDAP_USER_BASE_DN is required")
        if "{username}" not in s.ldap_user_filter:
            raise DirectoryConfigError("LDAP_USER_FILTER must contain {username}")
        if s.ldap_group_base_dn and "{user_dn}" not in s.ldap_group_filter:
            raise DirectoryConfigError("LDAP_GROUP_FILTER must contain {user_dn}")
        encrypted = s.ldap_url.lower().startswith("ldaps://") or s.ldap_start_tls
        if not encrypted and not s.ldap_allow_plaintext:
            raise DirectoryConfigError(
                "Directory passwords would be sent unencrypted. Use ldaps:// or "
                "LDAP_START_TLS=true (LDAP_ALLOW_PLAINTEXT=true only for isolated tests)."
            )
        self._s = s
        self._connect = connection_factory
        self._server = server or self._make_server()

    def _make_server(self) -> Server:
        s = self._s
        # Certificate and host name are always verified.
        tls = Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=s.ldap_ca_file or None)
        return Server(
            s.ldap_url,
            use_ssl=s.ldap_url.lower().startswith("ldaps://"),
            tls=tls,
            get_info=NONE,
            connect_timeout=s.ldap_timeout_s,
        )

    def _bind(self, user: str, password: str) -> tuple[Connection, bool]:
        conn = self._connect(self._server, user, password)
        if self._s.ldap_start_tls:
            conn.open()
            if not conn.start_tls():
                raise DirectoryUnavailableError("StartTLS failed (check LDAP_CA_FILE)")
        return conn, bool(conn.bind())

    def authenticate(self, username: str, password: str) -> DirectoryUser | None:
        if not username or not password:
            return None  # never let an empty password reach a bind (anonymous-bind bypass)
        s = self._s
        service: Connection | None = None
        user_conn: Connection | None = None
        try:
            service, ok = self._bind(s.ldap_bind_dn, s.ldap_bind_password)
            if not ok:
                raise DirectoryUnavailableError(
                    f"Directory refused the service account ({service.result.get('description')})"
                )
            service.search(
                s.ldap_user_base_dn,
                s.ldap_user_filter.format(username=escape_filter_chars(username)),
                search_scope=SUBTREE,
                attributes=[s.ldap_display_name_attr],
                size_limit=2,
            )
            entries = list(service.entries)
            if len(entries) != 1:
                if len(entries) > 1:
                    log.warning("Directory sign-in: '%s' matches several entries", username)
                return None
            entry = entries[0]
            dn: str = entry.entry_dn

            user_conn, ok = self._bind(dn, password)
            if not ok:
                return None

            display = _first(entry, s.ldap_display_name_attr)
            groups: set[str] = set()
            if s.ldap_group_base_dn:
                service.search(
                    s.ldap_group_base_dn,
                    s.ldap_group_filter.format(user_dn=escape_filter_chars(dn)),
                    search_scope=SUBTREE,
                    attributes=[],
                )
                groups = {normalise_dn(e.entry_dn) for e in service.entries}
            return DirectoryUser(username, dn, display, frozenset(groups))
        except LDAPException as exc:
            raise DirectoryUnavailableError(f"Directory error: {exc}") from exc
        finally:
            for conn in (user_conn, service):
                if conn is not None:
                    with contextlib.suppress(LDAPException):
                        conn.unbind()


def _first(entry: Any, attr: str) -> str | None:
    try:
        value = entry[attr].value
    except (KeyError, IndexError, LDAPException):
        return None
    if isinstance(value, list):
        value = value[0] if value else None
    return str(value) if value else None
