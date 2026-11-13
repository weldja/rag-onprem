"""Build Outlook .msg files for tests (and the eval corpus): the MAPI property
streams of MS-OXMSG, packed with app.ingestion.cfb."""

import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.ingestion.cfb import write_cfb


@dataclass
class Att:
    filename: str
    data: bytes = b""
    hidden: bool = False
    content_id: str = ""
    message: "Msg | None" = None  # an attached email instead of a file


@dataclass
class Msg:
    subject: str = ""
    body: str | None = None
    html: str | None = None
    rtf: bytes | None = None  # uncompressed RTF; stored compressed, as Outlook does
    sender_name: str = ""
    sender_smtp: str = ""
    to: str = ""
    cc: str = ""
    date: datetime | None = None
    attachments: list[Att] = field(default_factory=list)


def _unicode(pid: int, value: str) -> tuple[str, bytes]:
    return f"__substg1.0_{pid:04X}001F", value.encode("utf-16-le")


def _binary(pid: int, value: bytes) -> tuple[str, bytes]:
    return f"__substg1.0_{pid:04X}0102", value


def _fixed(pid: int, ptype: int, value: bytes) -> bytes:
    return struct.pack("<II8s", (pid << 16) | ptype, 0x6, value.ljust(8, b"\0"))


def _filetime(when: datetime) -> bytes:
    ticks = int((when - datetime(1601, 1, 1, tzinfo=UTC)).total_seconds() * 10_000_000)
    return struct.pack("<Q", ticks)


def _message(m: Msg, header: int) -> dict[str, bytes]:
    streams: dict[str, bytes] = {}
    for pid, value in (
        (0x0037, m.subject),
        (0x0C1A, m.sender_name),
        (0x5D01, m.sender_smtp),
        (0x0E04, m.to),
        (0x0E03, m.cc),
    ):
        if value:
            name, data = _unicode(pid, value)
            streams[name] = data
    if m.body is not None:
        name, data = _unicode(0x1000, m.body)
        streams[name] = data
    if m.html is not None:
        name, data = _binary(0x1013, m.html.encode("utf-8"))
        streams[name] = data
    if m.rtf is not None:
        import compressed_rtf

        name, data = _binary(0x1009, compressed_rtf.compress(m.rtf))
        streams[name] = data
    props = b"\0" * header
    props += _fixed(0x3FDE, 0x0003, struct.pack("<I", 65001))  # UTF-8
    if m.date:
        props += _fixed(0x0039, 0x0040, _filetime(m.date))
    streams["__properties_version1.0"] = props
    for n, a in enumerate(m.attachments):
        prefix = f"__attach_version1.0_#{n:08X}/"
        aprops = b"\0" * 8
        name, data = _unicode(0x3707, a.filename)
        streams[prefix + name] = data
        if a.message is not None:
            aprops += _fixed(0x3705, 0x0003, struct.pack("<I", 5))
            for path, data in _message(a.message, 24).items():
                streams[prefix + "__substg1.0_3701000D/" + path] = data
        else:
            aprops += _fixed(0x3705, 0x0003, struct.pack("<I", 1))
            name, data = _binary(0x3701, a.data)
            streams[prefix + name] = data
        if a.hidden:
            aprops += _fixed(0x7FFE, 0x000B, struct.pack("<I", 1))
        if a.content_id:
            name, data = _unicode(0x3712, a.content_id)
            streams[prefix + name] = data
        streams[prefix + "__properties_version1.0"] = aprops
    return streams


def build_msg(m: Msg) -> bytes:
    return write_cfb(_message(m, 32))
