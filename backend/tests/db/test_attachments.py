"""Email attachments as documents of their own, against real SQL: creation,
sharing, access control, updates, share emails, deletion and duplicates."""

import io
import os
from pathlib import Path

import docx
import pytest
from sqlalchemy import Engine, select

from app.auth import accounts
from app.auth.acl import Viewer
from app.core.config import Settings
from app.db.tables import document_groups, documents, ingest_jobs
from app.ingestion import registry, shares
from app.ingestion.attachments import SEPARATOR
from app.ingestion.pipeline import process_document
from app.retrieval.search import hybrid_search
from tests.msgfile import Att, Msg, build_msg
from tests.pdfgen import make_pdf

pytestmark = [pytest.mark.db, pytest.mark.enable_socket]

PASSWORD = "a-long-test-password"


class _Embedder:
    """Every text gets the same vector, so retrieval is decided by the ACL alone."""

    def __init__(self, dim: int) -> None:
        self.dim = dim

    @property
    def embed_model(self) -> str:
        return "test-embed"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (self.dim - 1) for _ in texts]


def _docx(text: str) -> bytes:
    d = docx.Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


INVOICE = make_pdf([["Invoice 1042", "Total due: 950 GBP, walruscode"]])


def _email(*, invoice: bytes = INVOICE, extra: list[Att] | None = None) -> bytes:
    nested = Msg(
        subject="Original quote",
        body="Quote attached.",
        attachments=[Att("quote.docx", _docx("Quote: 12 licences, narwhalcode"))],
    )
    return build_msg(
        Msg(
            subject="Invoice and quote",
            body="Please pay the invoice. Mailcode penguincode.",
            sender_name="Dana",
            attachments=[
                Att("invoice.pdf", invoice),
                Att("logo.png", b"\x89PNG", content_id="logo@1"),
                Att("archive.zip", b"PK\x03\x04"),
                Att("fw", message=nested),
                *(extra or []),
            ],
        )
    )


@pytest.fixture
def env(db: Engine, tmp_path: Path) -> dict[str, object]:
    settings = Settings(upload_dir=str(tmp_path / "uploads"), embed_model="test-embed")
    finance = accounts.create_group(db, "Finance")
    hr = accounts.create_group(db, "HR")
    fiona = accounts.create_user(
        db, settings, username="fiona", password=PASSWORD, group_ids=[finance]
    )
    harry = accounts.create_user(db, settings, username="harry", password=PASSWORD, group_ids=[hr])
    return {"settings": settings, "finance": finance, "hr": hr, "fiona": fiona, "harry": harry}


def _upload(db: Engine, env: dict[str, object], tmp_path: Path, data: bytes) -> int:
    path = tmp_path / "mail.msg"
    path.write_bytes(data)
    doc_id, _ = registry.register(
        db,
        filename="mail.msg",
        sha256=str(abs(hash(data))).ljust(64, "0")[:64],
        ext="msg",
        size=len(data),
        storage_path=str(path),
        enqueue=False,
        uploaded_by=env["fiona"],  # type: ignore[arg-type]
        group_ids=[env["finance"]],  # type: ignore[list-item]
    )
    return doc_id


def _index(db: Engine, env: dict[str, object], doc_id: int) -> int:
    s = env["settings"]
    assert isinstance(s, Settings)
    return process_document(db, doc_id, embedder=_Embedder(s.embed_dim), settings=s)


def _children(db: Engine, parent: int) -> dict[str, dict[str, object]]:
    with db.connect() as conn:
        rows = conn.execute(select(documents).where(documents.c.parent_id == parent)).mappings()
        return {str(r["filename"]): dict(r) for r in rows}


def _index_all(db: Engine, env: dict[str, object]) -> None:
    """Index every pending document (attachments of attachments too)."""
    while True:
        with db.connect() as conn:
            pending = conn.execute(
                select(documents.c.id).where(documents.c.status == "pending")
            ).scalars()
            ids = list(pending)
        if not ids:
            return
        for i in ids:
            _index(db, env, i)


def _viewer(db: Engine, user_id: object) -> Viewer:
    u = accounts.get_user(db, int(user_id))  # type: ignore[call-overload]
    return Viewer(u.id, u.username, u.is_admin)


def _search(db: Engine, viewer: Viewer, word: str) -> set[str]:
    hits = hybrid_search(
        db,
        word,
        [1.0] + [0.0] * (Settings().embed_dim - 1),
        viewer=viewer,
        embed_model="test-embed",
        k=20,
        candidates=30,
        min_similarity=0.1,
        keyword_keep=5,
    )
    return {h.filename for h in hits if word in h.text}


def test_supported_attachments_become_child_documents(
    db: Engine, env: dict[str, object], tmp_path: Path
) -> None:
    email = _upload(db, env, tmp_path, _email())
    assert _index(db, env, email) >= 1
    kids = _children(db, email)
    # The logo is an inline image and the zip is unsupported: both skipped.
    assert set(kids) == {"invoice.pdf", "Original quote.msg"}
    for kid in kids.values():
        assert kid["status"] == "pending" and kid["uploaded_by"] == env["fiona"]
        assert Path(str(kid["storage_path"])).is_file()
    with db.connect() as conn:
        groups = set(
            conn.execute(
                select(document_groups.c.group_id).where(
                    document_groups.c.document_id.in_([k["id"] for k in kids.values()])
                )
            ).scalars()
        )
        jobs = conn.execute(select(ingest_jobs.c.document_id)).scalars().all()
    assert groups == {env["finance"]}
    assert sorted(jobs) == sorted(k["id"] for k in kids.values())

    # Indexing the attached email adds its own attachment (nested).
    _index_all(db, env)
    nested = _children(db, int(kids["Original quote.msg"]["id"]))  # type: ignore[call-overload]
    assert set(nested) == {"quote.docx"} and nested["quote.docx"]["status"] == "ready"


def test_attachment_chunks_follow_the_emails_groups(
    db: Engine, env: dict[str, object], tmp_path: Path
) -> None:
    email = _upload(db, env, tmp_path, _email())
    _index_all(db, env)
    fiona, harry = _viewer(db, env["fiona"]), _viewer(db, env["harry"])
    assert _search(db, fiona, "walruscode") == {"invoice.pdf"}
    assert _search(db, fiona, "narwhalcode") == {"quote.docx"}
    assert _search(db, harry, "walruscode") == set()
    assert _search(db, harry, "narwhalcode") == set()

    # Re-sharing the email re-shares its attachments, at every depth.
    registry.set_groups(db, email, [int(env["hr"])], Viewer(0, "admin", True))  # type: ignore[call-overload]
    assert _search(db, harry, "narwhalcode") == {"quote.docx"}
    # Fiona uploaded the email: an uploader always keeps access to it and its attachments.
    assert _search(db, fiona, "walruscode") == {"invoice.pdf"}


def test_attachments_cant_be_deleted_or_reshared_on_their_own(
    db: Engine, env: dict[str, object], tmp_path: Path
) -> None:
    email = _upload(db, env, tmp_path, _email())
    _index(db, env, email)
    child = int(_children(db, email)["invoice.pdf"]["id"])  # type: ignore[call-overload]
    admin = Viewer(0, "admin", True)
    with pytest.raises(registry.ConflictError):
        registry.delete_document(db, child, admin)
    with pytest.raises(registry.ConflictError):
        registry.set_groups(db, child, [], admin)


def test_reindexing_updates_adds_and_removes_attachments(
    db: Engine, env: dict[str, object], tmp_path: Path
) -> None:
    email = _upload(db, env, tmp_path, _email())
    _index_all(db, env)
    before = _children(db, email)
    old_invoice = Path(str(before["invoice.pdf"]["storage_path"]))

    new_invoice = make_pdf([["Invoice 1042 (corrected)", "Total due: 905 GBP"]])
    data = _email(invoice=new_invoice, extra=[Att("minutes.txt", b"Board minutes")])
    (tmp_path / "mail.msg").write_bytes(data)
    _index(db, env, email)
    after = _children(db, email)
    assert set(after) == {"invoice.pdf", "Original quote.msg", "minutes.txt"}
    assert after["invoice.pdf"]["id"] == before["invoice.pdf"]["id"]  # same document, new file
    assert after["invoice.pdf"]["status"] == "pending"
    assert after["Original quote.msg"]["status"] == "ready"  # unchanged: not re-indexed
    assert not old_invoice.exists()  # no other document used the old file

    (tmp_path / "mail.msg").write_bytes(build_msg(Msg(subject="Invoice and quote", body="x")))
    _index(db, env, email)
    assert _children(db, email) == {}


def test_deleting_the_email_deletes_its_attachments_and_their_files(
    db: Engine, env: dict[str, object], tmp_path: Path
) -> None:
    email = _upload(db, env, tmp_path, _email())
    _index_all(db, env)
    with db.connect() as conn:
        files = [
            Path(p)
            for p in conn.execute(
                select(documents.c.storage_path).where(documents.c.parent_id.is_not(None))
            ).scalars()
        ]
    own, attached = registry.delete_document(db, email, Viewer(0, "admin", True))
    assert own == str(tmp_path / "mail.msg") and sorted(attached) == sorted(map(str, files))
    with db.connect() as conn:
        assert conn.execute(select(documents.c.id)).first() is None


def test_identical_attachments_share_chunks_and_files(
    db: Engine, env: dict[str, object], tmp_path: Path
) -> None:
    first = _upload(db, env, tmp_path, _email())
    _index_all(db, env)
    other = tmp_path / "other"
    other.mkdir()
    second = _upload(db, env, other, _email(extra=[Att("note.txt", b"different email")]))
    _index_all(db, env)
    a, b = _children(db, first)["invoice.pdf"], _children(db, second)["invoice.pdf"]
    assert a["sha256"] == b["sha256"] and a["storage_path"] == b["storage_path"]
    assert a["status"] == b["status"] == "ready"


def test_share_emails_attachments_follow_the_share(db: Engine, tmp_path: Path) -> None:
    root = tmp_path / "1"
    (root / "Inbox").mkdir(parents=True)
    (root / "Inbox" / "mail.msg").write_bytes(_email())
    s = Settings(
        share_1_path="/srv/mail",
        share_1_groups="Finance",
        shares_mount_dir=str(tmp_path),
        upload_dir=str(tmp_path / "uploads"),
        embed_model="test-embed",
    )
    env = {"settings": s}
    shares.sync_sources(db, s.shares())
    shares.scan_due(db, s, force=True)
    with db.connect() as conn:
        email = conn.execute(select(documents.c.id)).scalar_one()
    _index(db, env, email)
    kids = _children(db, email)
    assert kids["invoice.pdf"]["source_path"] == f"Inbox/mail.msg{SEPARATOR}invoice.pdf"
    assert kids["invoice.pdf"]["source_id"] is not None

    # A rescan neither deletes the attachments nor re-reads an unchanged email.
    ((_, result),) = shares.scan_due(db, s, force=True)
    assert (result.removed, result.changed, result.unchanged) == (0, 0, 1)
    assert set(_children(db, email)) == {"invoice.pdf", "Original quote.msg"}

    # Changing the email's file is what re-reads it (size/modification time).
    path = root / "Inbox" / "mail.msg"
    path.write_bytes(_email(extra=[Att("minutes.txt", b"Board minutes")]))
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 10))
    ((_, result),) = shares.scan_due(db, s, force=True)
    assert result.changed == 1
    with pytest.raises(registry.ConflictError):
        registry.delete_document(db, int(kids["invoice.pdf"]["id"]), Viewer(0, "a", True))  # type: ignore[call-overload]
