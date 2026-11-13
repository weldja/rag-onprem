"""Outlook .msg reading (app.ingestion.msg) and the compound-file writer."""

import io
from datetime import UTC, datetime
from pathlib import Path

import docx
import olefile
import pytest

from app.ingestion.cfb import write_cfb
from app.ingestion.loaders import LoadError, load_file
from app.ingestion.msg import MsgError, read_msg
from tests.msgfile import Att, Msg, build_msg
from tests.pdfgen import make_pdf

WHEN = datetime(2026, 3, 4, 14, 5, tzinfo=UTC)


def _docx(text: str) -> bytes:
    d = docx.Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _write(tmp_path: Path, m: Msg, name: str = "mail.msg") -> Path:
    path = tmp_path / name
    path.write_bytes(build_msg(m))
    return path


def test_cfb_round_trip_small_large_and_nested_streams() -> None:
    big = bytes(range(256)) * 40  # 10 KB: regular sectors, not the mini stream
    huge = b"x" * (8 * 1024 * 1024)  # needs DIFAT sectors beyond the header's 109
    data = write_cfb({"a": b"small", "dir/b": big, "dir/sub/c": b"", "z": huge})
    ole = olefile.OleFileIO(data)
    assert ole.openstream("a").read() == b"small"
    assert ole.openstream("dir/b").read() == big
    assert ole.openstream("dir/sub/c").read() == b""
    assert ole.openstream("z").read() == huge
    assert ole.get_type("dir/sub") == olefile.STGTY_STORAGE


def test_reads_headers_body_and_date(tmp_path: Path) -> None:
    mail = read_msg(
        str(
            _write(
                tmp_path,
                Msg(
                    subject="Q3 budget sign-off",
                    body="Hi all,\r\n\r\n\r\nThe Q3 budget is approved at £48,000.\r\n",
                    sender_name="Dana Price",
                    sender_smtp="dana@example.com",
                    to="Finance Team",
                    cc="Ops",
                    date=WHEN,
                ),
            )
        )
    )
    assert mail.subject == "Q3 budget sign-off"
    assert mail.sender == "Dana Price <dana@example.com>"
    assert (mail.to, mail.cc) == ("Finance Team", "Ops")
    assert mail.date == WHEN
    assert mail.body == "Hi all,\n\nThe Q3 budget is approved at £48,000."
    assert mail.label == "Q3 budget sign-off (04 Mar 2026)"
    text = mail.text()
    assert text.startswith("Subject: Q3 budget sign-off\nFrom: Dana Price <dana@example.com>")
    assert "Date: 04 March 2026 14:05 UTC" in text


def test_link_targets_are_dropped_and_long_urls_shortened(tmp_path: Path) -> None:
    safelink = "https://eur01.safelinks.protection.outlook.com/?url=https%3A%2F%2Fx.example" + (
        "%2Fa" * 60
    )
    body = (
        f"Join the meeting now <{safelink}>\r\n"
        "Mail me <mailto:dana@example.com> or call <tel:+441234>.\r\n"
        f"Raw link: {safelink}\r\nShort link: https://intranet/leave stays."
    )
    mail = read_msg(str(_write(tmp_path, Msg(subject="Links", body=body))))
    assert mail.body == (
        "Join the meeting now\nMail me or call.\n"
        "Raw link: https://eur01.safelinks.protection.outlook.com/…\n"
        "Short link: https://intranet/leave stays."
    )


def test_email_with_no_body_still_has_its_headers(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        Msg(subject="Signed contract", sender_name="Legal", attachments=[Att("c.pdf", b"%PDF")]),
    )
    loaded = load_file(path, "msg")
    assert loaded.email is not None and loaded.email.body == ""
    assert loaded.pages[0].page_content == (
        "Subject: Signed contract\nFrom: Legal\nAttachments: c.pdf"
    )
    assert loaded.pages[0].metadata["section"] == "Signed contract"


def test_html_only_body_is_read_as_text(tmp_path: Path) -> None:
    html = (
        "<html><head><style>p{color:red}</style><title>x</title></head><body>"
        "<p>Parking moves to <b>bay 7</b> from Monday.</p><script>alert(1)</script></body></html>"
    )
    mail = read_msg(str(_write(tmp_path, Msg(subject="Parking", html=html))))
    assert mail.body == "Parking moves to\nbay 7\nfrom Monday."


def test_rtf_only_body_is_decompressed_and_read(tmp_path: Path) -> None:
    rtf = rb"{\rtf1\ansi\deff0 {\fonttbl{\f0 Arial;}}\f0 The server move is on \b Friday\b0 .\par}"
    mail = read_msg(str(_write(tmp_path, Msg(subject="Move", rtf=rtf))))
    assert "The server move is on Friday." in mail.body


def test_attachments_inline_images_and_unsupported_types(tmp_path: Path) -> None:
    pdf = make_pdf([["Invoice 1042", "Total due: 950 GBP"]])
    path = _write(
        tmp_path,
        Msg(
            subject="Invoice",
            body="See attached.",
            attachments=[
                Att("invoice.pdf", pdf),
                Att("logo.png", b"\x89PNG", content_id="logo@1"),  # signature image
                Att("hidden.txt", b"x", hidden=True),
                Att("archive.zip", b"PK"),
                Att("..\\..\\etc/passwd.docx", _docx("x")),  # path in the name
            ],
        ),
    )
    mail = read_msg(str(path))
    by_name = {a.filename: a for a in mail.attachments}
    assert by_name["invoice.pdf"].data == pdf and not by_name["invoice.pdf"].inline
    assert by_name["logo.png"].inline and by_name["hidden.txt"].inline
    assert "archive.zip" in by_name  # kept here; the pipeline skips unsupported types
    assert "passwd.docx" in by_name  # no folders in attachment names
    expected = "Attachments: invoice.pdf, archive.zip, passwd.docx\n\nSee attached."
    assert mail.text().endswith(expected)


def test_attached_email_is_saved_as_a_msg_with_its_own_attachments(tmp_path: Path) -> None:
    inner = Msg(
        subject="Original quote",
        body="Quote attached.",
        date=WHEN,
        attachments=[Att("quote.docx", _docx("Quote: 12 licences at 40 GBP"))],
    )
    outer = Msg(subject="Fw: Original quote", body="FYI", attachments=[Att("x", message=inner)])
    mail = read_msg(str(_write(tmp_path, outer)))
    [att] = mail.attachments
    assert att.is_message and att.filename == "Original quote.msg"
    assert att.data is not None
    nested = read_msg(att.data)  # a standalone .msg
    assert (nested.subject, nested.body, nested.date) == ("Original quote", "Quote attached.", WHEN)
    [quote] = nested.attachments
    assert quote.filename == "quote.docx"
    nested_path = tmp_path / "quote.docx"
    nested_path.write_bytes(quote.data or b"")
    assert "12 licences" in load_file(nested_path, "docx").pages[0].page_content


def test_not_a_msg_file(tmp_path: Path) -> None:
    path = tmp_path / "fake.msg"
    path.write_bytes(b"From: someone\n\nplain text pretending to be an email")
    with pytest.raises(MsgError):
        read_msg(str(path))
    with pytest.raises(LoadError):
        load_file(path, "msg")
