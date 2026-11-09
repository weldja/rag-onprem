"""File shares against real SQL: discovery, change detection, the safety
guards, and access control for share documents."""

import os
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, insert, select

from app.auth import accounts
from app.auth.acl import Viewer
from app.core.config import Settings
from app.db.tables import chunks, documents, ingest_jobs, sources
from app.ingestion import registry, shares
from app.ingestion.pipeline import copy_from_duplicate
from app.retrieval.search import hybrid_search

pytestmark = [pytest.mark.db, pytest.mark.enable_socket]

PASSWORD = "a-long-test-password"


def _settings(root: Path, **extra: str) -> Settings:
    return Settings(
        share_1_path="/srv/policies",
        share_1_name="Policies",
        share_1_display_path="\\\\fs01\\policies",
        share_1_groups="Finance",
        shares_mount_dir=str(root),
        **extra,
    )


def _scan(db: Engine, s: Settings) -> shares.ScanResult:
    shares.sync_sources(db, s.shares())
    ((_, result),) = shares.scan_due(db, s, force=True)
    return result


def _docs(db: Engine) -> dict[str, dict[str, object]]:
    with db.connect() as conn:
        rows = conn.execute(select(documents)).mappings().all()
    return {str(r["source_path"]): dict(r) for r in rows}


def _jobs(db: Engine) -> int:
    with db.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(ingest_jobs)).scalar_one())


@pytest.fixture
def share(tmp_path: Path) -> Path:
    root = tmp_path / "1"  # Settings mount share 1 at <shares_mount_dir>/1
    (root / "HR").mkdir(parents=True)
    (root / "leave.txt").write_text("Annual leave: 25 days")
    (root / "HR" / "pay.txt").write_text("Pay bands")
    (root / "~$leave.docx").write_text("lock file")
    return root


def test_scan_registers_queues_and_shares_with_the_configured_groups(
    db: Engine, share: Path
) -> None:
    result = _scan(db, _settings(share.parent))
    assert (result.added, result.error) == (2, None)
    docs = _docs(db)
    assert sorted(docs) == ["HR/pay.txt", "leave.txt"]
    assert docs["leave.txt"]["storage_path"] == str(share / "leave.txt")
    assert docs["leave.txt"]["status"] == "pending"
    assert _jobs(db) == 2
    finance = next(g for g in accounts.list_groups(db) if g.name == "Finance")  # created
    infos = registry.list_documents(db, "bge-m3", Viewer(1, "root", True))
    assert all(i.group_ids == [finance.id] for i in infos)
    with db.connect() as conn:
        src = conn.execute(select(sources)).mappings().one()
    assert (src["name"], src["display_path"], src["files_seen"]) == (
        "Policies",
        "\\\\fs01\\policies",
        2,
    )


def test_rescan_detects_changed_and_deleted_files_only(db: Engine, share: Path) -> None:
    s = _settings(share.parent)
    _scan(db, s)
    assert _scan(db, s).unchanged == 2 and _jobs(db) == 2  # nothing re-read

    # Same content, new timestamp: not re-indexed.
    os.utime(share / "leave.txt", (1_000_000_000, 1_000_000_000))
    again = _scan(db, s)
    assert (again.changed, again.unchanged) == (0, 2) and _jobs(db) == 2

    (share / "leave.txt").write_text("Annual leave: 30 days")
    (share / "HR" / "pay.txt").unlink()
    result = _scan(db, s)
    assert (result.changed, result.removed) == (1, 1)
    assert sorted(_docs(db)) == ["leave.txt"]
    # leave.txt's queued (never run) job will read the new content; pay.txt's job went with it
    assert _jobs(db) == 1


def test_unmounted_or_empty_share_removes_nothing(db: Engine, share: Path) -> None:
    s = _settings(share.parent)
    _scan(db, s)
    for f in [share / "leave.txt", share / "HR" / "pay.txt"]:
        f.unlink()
    result = _scan(db, s)
    assert result.removed == 0 and result.error and "empty" in result.error
    assert len(_docs(db)) == 2
    (share / "HR").rmdir()
    (share / "~$leave.docx").unlink()
    share.rmdir()
    result = _scan(db, s)
    assert result.error and "mounted" in result.error and len(_docs(db)) == 2


def test_excluded_folders_are_dropped_from_the_index(db: Engine, share: Path) -> None:
    _scan(db, _settings(share.parent))
    result = _scan(db, _settings(share.parent, share_1_exclude="HR"))
    assert result.removed == 1 and sorted(_docs(db)) == ["leave.txt"]


def test_share_documents_follow_acl_and_cannot_be_deleted_or_reshared(
    db: Engine, share: Path
) -> None:
    settings = Settings()
    _scan(db, _settings(share.parent))
    finance = next(g.id for g in accounts.list_groups(db) if g.name == "Finance")
    hr = accounts.create_group(db, "HR")
    fiona = accounts.create_user(
        db, settings, username="fiona", password=PASSWORD, group_ids=[finance]
    )
    harry = accounts.create_user(db, settings, username="harry", password=PASSWORD, group_ids=[hr])
    admin = Viewer(99, "root", True)
    docs = _docs(db)
    dim = settings.embed_dim
    with db.begin() as conn:
        for i, d in enumerate(docs.values()):
            conn.execute(
                insert(chunks).values(
                    document_id=d["id"],
                    ordinal=0,
                    text=f"share text {i}",
                    embedding=[1.0] + [0.0] * (dim - 1),
                    embed_model=settings.embed_model,
                )
            )
        conn.exec_driver_sql("UPDATE documents SET status = 'ready'")

    def search(viewer: Viewer) -> int:
        return len(
            hybrid_search(
                db,
                "share",
                [1.0] + [0.0] * (dim - 1),
                viewer=viewer,
                embed_model=settings.embed_model,
                k=10,
                candidates=30,
                min_similarity=0.1,
                keyword_keep=5,
            )
        )

    assert search(Viewer(fiona, "fiona", False)) == 2
    assert search(Viewer(harry, "harry", False)) == 0
    # Harry doesn't learn the share exists; Fiona and admins see it.
    assert registry.list_sources(db, Viewer(harry, "harry", False)) == []
    assert [s.name for s in registry.list_sources(db, Viewer(fiona, "fiona", False))] == [
        "Policies"
    ]
    doc_id = int(docs["leave.txt"]["id"])
    with pytest.raises(registry.ConflictError):
        registry.delete_document(db, doc_id, admin)
    with pytest.raises(registry.ConflictError):
        registry.set_groups(db, doc_id, [hr], admin)


def test_unconfigured_share_is_kept_until_removed(db: Engine, share: Path) -> None:
    _scan(db, _settings(share.parent))
    shares.sync_sources(db, [])  # SHARE_1_PATH removed from .env
    (src,) = registry.list_sources(db, Viewer(1, "root", True))
    assert src.active is False and len(_docs(db)) == 2
    registry.remove_source(db, src.id)
    assert _docs(db) == {}


def test_identical_files_copy_chunks_instead_of_re_embedding(db: Engine, share: Path) -> None:
    (share / "copy-of-leave.txt").write_text("Annual leave: 25 days")
    _scan(db, _settings(share.parent))
    docs = _docs(db)
    original, copy = int(docs["leave.txt"]["id"]), int(docs["copy-of-leave.txt"]["id"])
    with db.begin() as conn:  # the worker marks it processing before copying
        conn.exec_driver_sql("UPDATE documents SET status = 'processing' WHERE id = %s", (copy,))
    assert copy_from_duplicate(db, copy, str(docs["copy-of-leave.txt"]["sha256"]), "m") is None
    with db.begin() as conn:
        conn.execute(
            insert(chunks).values(
                document_id=original,
                ordinal=0,
                text="Annual leave: 25 days",
                embedding=[0.5] * Settings().embed_dim,
                embed_model="m",
            )
        )
        conn.exec_driver_sql("UPDATE documents SET status = 'ready' WHERE id = %s", (original,))
    assert copy_from_duplicate(db, copy, str(docs["copy-of-leave.txt"]["sha256"]), "m") == 1
    assert _docs(db)["copy-of-leave.txt"]["status"] == "ready"
    # A copy embedded with another model is not a donor.
    assert copy_from_duplicate(db, copy, str(docs["copy-of-leave.txt"]["sha256"]), "other") is None


def test_upload_dedupe_ignores_share_copies(db: Engine, share: Path) -> None:
    _scan(db, _settings(share.parent))
    sha = str(_docs(db)["leave.txt"]["sha256"])
    assert registry.find_by_sha(db, sha) is None
    doc_id, created = registry.register(
        db,
        filename="leave.txt",
        sha256=sha,
        ext="txt",
        size=21,
        storage_path="/data/uploads/x.txt",
        enqueue=False,
    )
    assert created and registry.find_by_sha(db, sha) == doc_id
