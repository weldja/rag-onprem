"""Outlook .msg files (MS-OXMSG): subject, sender, recipients, date, body and
attachments.

Written on olefile (BSD), compressed-rtf (MIT) and striprtf (BSD-3-Clause)
rather than extract-msg, which is GPL-3.0. All three are pure Python and make
no network calls.

A .msg file is a compound file. Each MAPI property is a stream named
__substg1.0_<tag><type> (strings, binary) or an entry in the fixed-size
__properties_version1.0 stream (dates, numbers). Attachments are storages
__attach_version1.0_#<n>; an attached email is a storage inside one of them,
with the same layout as a top-level message.
"""

import email.utils
import logging
import re
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import PurePath
from typing import Any

import olefile

from app.ingestion.cfb import write_cfb

log = logging.getLogger(__name__)

_PROPS = "__properties_version1.0"
_ATTACH = "__attach_version1.0_#"
_EMBEDDED = "__substg1.0_3701000D"

# Property ids
PR_SUBJECT = 0x0037
PR_CLIENT_SUBMIT_TIME = 0x0039
PR_SENT_REPRESENTING_NAME = 0x0042
PR_SENT_REPRESENTING_EMAIL = 0x0065
PR_TRANSPORT_HEADERS = 0x007D
PR_SENDER_NAME = 0x0C1A
PR_SENDER_EMAIL = 0x0C1F
PR_SENDER_SMTP = 0x5D01
PR_DISPLAY_CC = 0x0E03
PR_DISPLAY_TO = 0x0E04
PR_DELIVERY_TIME = 0x0E06
PR_BODY = 0x1000
PR_RTF_COMPRESSED = 0x1009
PR_HTML = 0x1013
PR_DISPLAY_NAME = 0x3001
PR_CREATION_TIME = 0x3007
PR_ATTACH_DATA = 0x3701
PR_ATTACH_FILENAME = 0x3704
PR_ATTACH_METHOD = 0x3705
PR_ATTACH_LONG_FILENAME = 0x3707
PR_ATTACH_CONTENT_ID = 0x3712
PR_INTERNET_CPID = 0x3FDE
PR_MESSAGE_CODEPAGE = 0x3FFD
PR_ATTACHMENT_HIDDEN = 0x7FFE

# Property types
PT_LONG = 0x0003
PT_BOOLEAN = 0x000B
PT_STRING8 = 0x001E
PT_UNICODE = 0x001F
PT_SYSTIME = 0x0040
PT_BINARY = 0x0102

ATTACH_EMBEDDED_MSG = 5


class MsgError(ValueError):
    """Not a readable Outlook message."""


@dataclass
class Attachment:
    key: str  # position among the message's attachments ("0", "1", ...)
    filename: str
    data: bytes | None = None  # the file, or the attached email saved as a .msg
    is_message: bool = False
    inline: bool = False  # hidden, or an image shown in the body (signature logos)


@dataclass
class Email:
    subject: str = ""
    sender: str = ""
    to: str = ""
    cc: str = ""
    date: datetime | None = None
    body: str = ""
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def label(self) -> str:
        """How citations show the email: subject and date."""
        subject = self.subject or "(no subject)"
        return f"{subject} ({self.date:%d %b %Y})" if self.date else subject

    def text(self) -> str:
        """Header lines, then the body: what gets indexed."""
        lines = [f"Subject: {self.subject or '(no subject)'}"]
        for name, value in (("From", self.sender), ("To", self.to), ("Cc", self.cc)):
            if value:
                lines.append(f"{name}: {value}")
        if self.date:
            lines.append(f"Date: {self.date:%d %B %Y %H:%M} UTC")
        names = [a.filename for a in self.attachments if not a.inline]
        if names:
            lines.append(f"Attachments: {', '.join(names)}")
        return "\n".join(lines) + ("\n\n" + self.body if self.body else "")


class _Reader:
    def __init__(self, ole: olefile.OleFileIO, prefix: list[str], header: int) -> None:
        self.ole = ole
        self.prefix = prefix
        self._fixed = self._fixed_props(header)

    def _path(self, name: str) -> list[str]:
        return [*self.prefix, name]

    def _stream(self, name: str) -> bytes | None:
        path = self._path(name)
        if not self.ole.exists(path) or self.ole.get_type(path) != olefile.STGTY_STREAM:
            return None
        data: bytes = self.ole.openstream(path).read()
        return data

    def _fixed_props(self, header: int) -> dict[int, tuple[int, bytes]]:
        """{property id: (type, 8-byte value)} from __properties_version1.0."""
        raw = self._stream(_PROPS) or b""
        props: dict[int, tuple[int, bytes]] = {}
        for off in range(header, len(raw) - 15, 16):
            tag, _flags, value = struct.unpack_from("<II8s", raw, off)
            props[tag >> 16] = (tag & 0xFFFF, value)
        return props

    def codepage(self) -> str:
        for pid in (PR_INTERNET_CPID, PR_MESSAGE_CODEPAGE):
            kind, value = self._fixed.get(pid, (0, b""))
            if kind == PT_LONG:
                cp = struct.unpack_from("<I", value)[0]
                if cp == 65001:
                    return "utf-8"
                if 0 < cp < 65000:
                    return f"cp{cp}"
        return "cp1252"

    def string(self, pid: int) -> str:
        raw = self._stream(f"__substg1.0_{pid:04X}{PT_UNICODE:04X}")
        if raw is not None:
            return raw.decode("utf-16-le", errors="replace").rstrip("\0").strip()
        raw = self._stream(f"__substg1.0_{pid:04X}{PT_STRING8:04X}")
        if raw is not None:
            try:
                return raw.decode(self.codepage(), errors="replace").rstrip("\0").strip()
            except LookupError:  # unknown code page
                return raw.decode("cp1252", errors="replace").rstrip("\0").strip()
        return ""

    def binary(self, pid: int) -> bytes | None:
        return self._stream(f"__substg1.0_{pid:04X}{PT_BINARY:04X}")

    def number(self, pid: int) -> int | None:
        kind, value = self._fixed.get(pid, (0, b""))
        if kind in (PT_LONG, PT_BOOLEAN):
            return int(struct.unpack_from("<I", value)[0])
        return None

    def time(self, pid: int) -> datetime | None:
        kind, value = self._fixed.get(pid, (0, b""))
        if kind != PT_SYSTIME:
            return None
        ticks = struct.unpack_from("<Q", value)[0]  # 100 ns since 1601-01-01 UTC
        if not ticks:
            return None
        try:
            return datetime(1601, 1, 1, tzinfo=UTC) + timedelta(microseconds=ticks // 10)
        except OverflowError:
            return None

    def children(self, marker: str) -> list[str]:
        """Names of the direct child storages starting with marker, in order."""
        depth = len(self.prefix)
        names = {
            entry[depth]
            for entry in self.ole.listdir(streams=True, storages=True)
            if len(entry) > depth + 1
            and entry[:depth] == self.prefix
            and entry[depth].startswith(marker)
        }
        return sorted(names)


def _html_to_text(raw: bytes | str, codepage: str) -> str:
    from bs4 import BeautifulSoup

    if isinstance(raw, bytes):
        try:
            html = raw.decode(codepage)
        except (UnicodeDecodeError, LookupError):
            html = raw.decode("utf-8", errors="replace")
    else:
        html = raw
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head", "title"]):
        tag.decompose()
    lines = (line.strip() for line in soup.get_text("\n").splitlines())
    return _tidy("\n".join(lines))


def _rtf_to_text(compressed: bytes) -> str:
    import compressed_rtf
    from striprtf.striprtf import rtf_to_text

    try:
        rtf: bytes = compressed_rtf.decompress(compressed)
    except Exception:  # damaged RTF: the email still has its headers
        log.warning("Compressed RTF body could not be read")
        return ""
    text: str = rtf_to_text(rtf.decode("latin-1"), errors="ignore")
    return _tidy(text)


# Outlook writes each link as "text <https://...>"; Office 365 wraps every URL
# in a ~400-character "safelinks" redirect. Link targets carry no answerable
# content, but cost the model ~2.5 characters per token to read and match
# keywords ("request", "data") in every question.
_LINK_TARGET = re.compile(r"\s*<(?:https?|mailto|tel|file):[^>\s]*>", re.IGNORECASE)
_LONG_URL = re.compile(r"(https?://[^/\s?#]+)[^\s<>\"']{40,}", re.IGNORECASE)


def _strip_links(text: str) -> str:
    """Drop link targets after link text, and cut bare long URLs to their host."""
    return _LONG_URL.sub(r"\1/…", _LINK_TARGET.sub("", text))


def _tidy(text: str) -> str:
    out: list[str] = []
    text = _strip_links(text)
    for line in (ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")):
        if line or (out and out[-1]):  # collapse runs of blank lines
            out.append(line)
    return "\n".join(out).strip()


def _body(r: _Reader) -> str:
    """Plain text if the message has it, else HTML, else RTF (Outlook often
    keeps only the RTF, sometimes with the HTML wrapped inside)."""
    body = r.string(PR_BODY)
    if body:
        return _tidy(body)
    html = r.binary(PR_HTML) or r.string(PR_HTML)
    if html:
        return _html_to_text(html, r.codepage())
    rtf = r.binary(PR_RTF_COMPRESSED)
    return _rtf_to_text(rtf) if rtf else ""


def _sender(r: _Reader) -> str:
    """Sender, else "sent on behalf of" (some messages only carry that)."""
    name = r.string(PR_SENDER_NAME) or r.string(PR_SENT_REPRESENTING_NAME)
    address = r.string(PR_SENDER_SMTP)
    if not address:
        candidate = r.string(PR_SENDER_EMAIL) or r.string(PR_SENT_REPRESENTING_EMAIL)
        address = candidate if "@" in candidate else ""
    if name and address and address.lower() not in name.lower():
        return f"{name} <{address}>"
    return name or address


def _date(r: _Reader) -> datetime | None:
    for pid in (PR_CLIENT_SUBMIT_TIME, PR_DELIVERY_TIME):
        if found := r.time(pid):
            return found
    for line in r.string(PR_TRANSPORT_HEADERS).splitlines():
        if line.lower().startswith("date:"):
            try:
                return email.utils.parsedate_to_datetime(line[5:].strip()).astimezone(UTC)
            except (TypeError, ValueError):
                break
    return r.time(PR_CREATION_TIME)


def _embedded_as_file(ole: olefile.OleFileIO, prefix: list[str]) -> bytes:
    """The attached email's storage, written out as a standalone .msg. Its
    property stream gets the 32-byte header of a top-level message (embedded
    messages have 24); nothing else changes."""
    streams: dict[str, bytes] = {}
    depth = len(prefix)
    for entry in ole.listdir(streams=True, storages=False):
        if entry[:depth] == prefix and len(entry) > depth:
            data: bytes = ole.openstream(entry).read()
            rel = entry[depth:]
            if rel == [_PROPS]:
                data = data[:24] + b"\0" * 8 + data[24:]
            streams["/".join(rel)] = data
    return write_cfb(streams)


def _message(ole: olefile.OleFileIO, prefix: list[str], header: int) -> Email:
    r = _Reader(ole, prefix, header)
    mail = Email(
        subject=r.string(PR_SUBJECT),
        sender=_sender(r),
        to=r.string(PR_DISPLAY_TO),
        cc=r.string(PR_DISPLAY_CC),
        date=_date(r),
        body=_body(r),
    )
    for n, storage in enumerate(r.children(_ATTACH)):
        a = _Reader(ole, [*prefix, storage], 8)
        name = (
            a.string(PR_ATTACH_LONG_FILENAME)
            or a.string(PR_ATTACH_FILENAME)
            or a.string(PR_DISPLAY_NAME)
        )
        embedded = [*prefix, storage, _EMBEDDED]
        is_storage = ole.exists(embedded) and ole.get_type(embedded) == olefile.STGTY_STORAGE
        if is_storage and a.number(PR_ATTACH_METHOD) in (ATTACH_EMBEDDED_MSG, None):
            # Saved as a .msg of its own; its attachments are read when that
            # file is indexed (each level is smaller, so nesting always ends).
            inner = _Reader(ole, embedded, 24)
            subject = inner.string(PR_SUBJECT) or name or "attached email"
            mail.attachments.append(
                Attachment(
                    key=str(n),
                    filename=_safe_name(subject, ".msg"),
                    data=_embedded_as_file(ole, embedded),
                    is_message=True,
                )
            )
            continue
        if is_storage:  # an embedded OLE object (method 6): nothing to index
            continue
        data = a.binary(PR_ATTACH_DATA)
        if data is None:  # linked file or OLE object: no contents to index
            continue
        name = name or f"attachment-{n + 1}"
        image = PurePath(name).suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif")
        inline = bool(a.number(PR_ATTACHMENT_HIDDEN)) or (
            image and bool(a.string(PR_ATTACH_CONTENT_ID))
        )
        mail.attachments.append(
            Attachment(key=str(n), filename=_safe_name(name), data=data, inline=inline)
        )
    return mail


def _safe_name(name: str, suffix: str = "") -> str:
    """A file name without folders or control characters."""
    base = PurePath(name.replace("\\", "/")).name
    base = "".join(c for c in base if c.isprintable() and c not in '<>:"|?*').strip(" .")
    base = (base or "attachment")[: 200 - len(suffix)]
    return base + suffix


def read_msg(source: Any) -> Email:
    """Parse a .msg file (path or bytes)."""
    try:
        if not olefile.isOleFile(source):
            raise MsgError("Not an Outlook message (.msg) file")
        ole = olefile.OleFileIO(source)
    except (OSError, ValueError) as exc:
        if isinstance(exc, MsgError):
            raise
        raise MsgError(f"Outlook message could not be read: {exc}") from exc
    try:
        if not any(e[0].startswith("__substg1.0_") for e in ole.listdir()):
            raise MsgError("Not an Outlook message (.msg) file")
        return _message(ole, [], 32)
    finally:
        ole.close()
