"""The Indexing page against real SQL: exclude / include / re-index, stopping a
running job, progress and queue position, and skipped share files."""

from pathlib import Path

import pytest
from sqlalchemy import Engine, func, insert, select

from app import worker
from app.auth import accounts
from app.auth.acl import Viewer
from app.core.config import Settings
from app.db.tables import chunks, documents, ingest_jobs
from app.ingestion import registry, shares
from tests.fakes import FakeEmbedder

pytestmark = [pytest.mark.db, pytest.mark.enable_socket]

ADMIN = Viewer(99, "root", True)


def _settings(root: Path, **extra: object) -> Settings:
    return Settings(
        share_1_path="/srv/policies",
        share_1_groups="All staff",
        shares_mount_dir=str(root),
        embed_dim=4,
        **extra,
    )


def _scan(db: Engine, s: Settings) -> shares.ScanResult:
    shares.sync_sources(db, s.shares())
    ((_, result),) = shares.scan_due(db, s, force=True)
    return result


def _doc(db: Engine, path: str) -> dict[str, object]:
    with db.connect() as conn:
        return dict(
            conn.execute(select(documents).where(documents.c.source_path == path)).mappings().one()
        )


def _id(db: Engine, path: str) -> int:
    with db.connect() as conn:
        return int(
            conn.execute(select(documents.c.id).where(documents.c.source_path == path)).scalar_one()
        )


def _jobs(db: Engine, doc_id: object) -> list[str]:
    with db.connect() as conn:
        return list(
            conn.execute(
                select(ingest_jobs.c.status)
                .where(ingest_jobs.c.document_id == doc_id)
                .order_by(ingest_jobs.c.id)
            ).scalars()
        )


def _chunks(db: Engine, doc_id: object) -> int:
    with db.connect() as conn:
        return int(
            conn.execute(
                select(func.count()).select_from(chunks).where(chunks.c.document_id == doc_id)
            ).scalar_one()
        )


def _make_ready(db: Engine, doc_id: object) -> None:
    with db.begin() as conn:
        conn.execute(
            insert(chunks).values(
                document_id=doc_id,
                ordinal=0,
                text="x",
                embedding=[1.0] * Settings().embed_dim,
                embed_model="m",
            )
        )
        conn.execute(documents.update().where(documents.c.id == doc_id).values(status="ready"))
        conn.execute(
            ingest_jobs.update().where(ingest_jobs.c.document_id == doc_id).values(status="done")
        )


@pytest.fixture
def share(tmp_path: Path) -> Path:
    root = tmp_path / "1"
    (root / "Archive").mkdir(parents=True)
    (root / "leave.txt").write_text("Annual leave: 25 days. " * 50)
    (root / "pay.txt").write_text("Pay bands")
    (root / "archive.zip").write_bytes(b"PK")
    (root / "empty.txt").write_bytes(b"")
    (root / "Archive" / "old.txt").write_text("old")
    return root


def test_skipped_files_are_listed_with_reasons(db: Engine, share: Path) -> None:
    result = _scan(db, _settings(share.parent, share_1_exclude="Archive"))
    assert result.skipped == 2  # .zip and the empty file; the excluded folder isn't counted
    assert [(s.path, s.reason) for s in registry.list_skipped(db)] == [
        ("Archive/", "excluded"),
        ("archive.zip", "unsupported"),
        ("empty.txt", "empty"),
    ]
    (share / "archive.zip").unlink()
    _scan(db, _settings(share.parent, share_1_exclude="Archive"))
    assert "archive.zip" not in [s.path for s in registry.list_skipped(db)]  # replaced each scan


def test_exclude_include_and_reindex(db: Engine, share: Path) -> None:
    s = _settings(share.parent)
    _scan(db, s)
    leave = _id(db, "leave.txt")
    _make_ready(db, leave)

    res = registry.apply_action(db, "exclude", [leave], ADMIN)
    assert res.changed == [leave] and _doc(db, "leave.txt")["status"] == "excluded"
    assert _chunks(db, leave) == 0
    assert registry.apply_action(db, "reindex", [leave], ADMIN).skipped == {
        leave: "excluded; include it to index it again"
    }

    # A changed file stays excluded and isn't queued.
    (share / "leave.txt").write_text("Annual leave: 30 days")
    _scan(db, s)
    assert _doc(db, "leave.txt")["status"] == "excluded" and _jobs(db, leave) == ["done"]

    res = registry.apply_action(db, "include", [leave], ADMIN)
    assert res.changed == [leave] and _doc(db, "leave.txt")["status"] == "pending"
    assert _jobs(db, leave) == ["done", "queued"]
    assert registry.apply_action(db, "reindex", [leave], ADMIN).skipped == {
        leave: "already queued or being indexed"
    }

    # Excluding a queued document cancels its job.
    registry.apply_action(db, "exclude", [leave], ADMIN)
    assert _jobs(db, leave) == ["done", "cancelled"]


def test_only_managers_can_change_indexing(db: Engine, share: Path) -> None:
    _scan(db, _settings(share.parent))
    staff = next(g.id for g in accounts.list_groups(db) if g.name == "All staff")
    uid = accounts.create_user(
        db, Settings(), username="sam", password="a-long-test-password", group_ids=[staff]
    )
    leave = _id(db, "leave.txt")
    res = registry.apply_action(db, "exclude", [leave, 12345], Viewer(uid, "sam", False))
    assert res.changed == []
    assert res.skipped == {
        leave: "only the uploader or an administrator can change it",
        12345: "not found",
    }
    assert _doc(db, "leave.txt")["status"] == "pending"


def test_queue_position_and_progress(db: Engine, share: Path) -> None:
    _scan(db, _settings(share.parent))
    infos = {i.source_path: i for i in registry.list_documents(db, "m", ADMIN)}
    assert sorted(i.queue_position for i in infos.values()) == [1, 2, 3]
    leave = infos["leave.txt"].id
    with db.begin() as conn:
        conn.execute(documents.update().where(documents.c.id == leave).values(status="processing"))
    assert registry.report_progress(db, leave, 3, 10)
    info = registry.get_document(db, leave, "m", ADMIN)
    assert (info.progress_done, info.progress_total) == (3, 10)


def test_restart_puts_interrupted_documents_back_in_the_queue(db: Engine, share: Path) -> None:
    _scan(db, _settings(share.parent))
    leave = _id(db, "leave.txt")
    assert registry.claim_job(db) is not None  # job 1 = leave.txt (oldest)
    with db.begin() as conn:
        conn.execute(documents.update().where(documents.c.id == leave).values(status="processing"))
    assert registry.requeue_stale_jobs(db, 0) == 1  # the worker died; it restarts
    assert _doc(db, "leave.txt")["status"] == "pending"
    assert registry.get_document(db, leave, "m", ADMIN).queue_position == 1


def test_excluding_a_running_document_stops_the_worker(db: Engine, share: Path) -> None:
    s = _settings(share.parent, chunk_size=200, chunk_overlap=0)
    _scan(db, s)
    leave = _id(db, "leave.txt")
    with db.begin() as conn:  # only leave.txt is queued
        conn.execute(
            ingest_jobs.update().where(ingest_jobs.c.document_id != leave).values(status="done")
        )

    class ExcludeMidway(FakeEmbedder):
        def embed(self, texts: list[str]) -> list[list[float]]:
            if len(self.calls) == 1:  # an administrator clicks Exclude during indexing
                registry.apply_action(db, "exclude", [leave], ADMIN)
            return super().embed(texts)

    embedder = ExcludeMidway()
    assert worker.run_once(db, embedder, s)
    assert len(embedder.calls) == 2  # stopped at the next excerpt, not after all of them
    assert _doc(db, "leave.txt")["status"] == "excluded"
    assert _chunks(db, leave) == 0
    assert _jobs(db, leave) == ["cancelled"]
