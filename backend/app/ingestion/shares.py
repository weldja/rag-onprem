"""File shares indexed in place: discover new, changed and deleted files and
queue them for the worker.

Design (adapted from rag-claude's docs-folder indexing, fixing its gaps):
- Shares are read-only bind mounts (/shares/1..3). Files are never copied into
  the uploads volume; documents.storage_path points at the file on the share.
- A scan stats every file and reads (hashes) one only when its size or
  modification time changed, so a periodic scan of a large network share is
  cheap. Unchanged content with a new timestamp is not re-indexed.
- os.walk with an error callback, never rglob: a folder renamed mid-scan or a
  folder without read permission must not abort the scan. If any part of the
  tree couldn't be read, nothing is deleted on that scan.
- An unmounted share looks like an empty folder. A scan that finds no files
  where documents exist is treated as "share unavailable", not "all deleted".
- Office lock files (~$x.docx), editor temp files, Thumbs.db, recycle bins and
  hidden files are skipped (rag-claude indexed ~$ lock files).
- Visibility comes from the share's configured groups and is re-applied on
  every scan, so changing SHARE_n_GROUPS takes effect without re-indexing.
"""

import fnmatch
import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import Engine, delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings, ShareConfig
from app.db.tables import documents, groups, ingest_jobs, source_skipped, sources
from app.ingestion.loaders import SUPPORTED_EXTENSIONS

log = logging.getLogger(__name__)

# Only one worker scans at a time (several worker replicas are allowed).
_SCAN_LOCK_ID = 0x5241_4753  # "RAGS"
_HASH_CHUNK = 1024 * 1024
_MAX_SKIPPED_LISTED = 10_000

_SKIP_DIRS = {"$recycle.bin", "#recycle", "@eadir", "system volume information", "__macosx"}
_SKIP_FILES = {"thumbs.db", "desktop.ini", ".ds_store"}


@dataclass(frozen=True)
class SourceRow:
    id: int
    key: str
    name: str
    root: str


@dataclass
class ScanResult:
    added: int = 0
    changed: int = 0
    removed: int = 0
    unchanged: int = 0
    skipped: int = 0  # unsupported, empty or too large
    error: str | None = None


def skip_name(name: str) -> bool:
    """Hidden, temporary and system files that are never documents."""
    lower = name.lower()
    return (
        lower.startswith((".", "~$", "~", ".~lock"))
        or lower.endswith((".tmp", ".partial", ".crdownload"))
        or lower in _SKIP_FILES
    )


def skip_dir(name: str) -> bool:
    return name.startswith((".", "~")) or name.lower() in _SKIP_DIRS


def excluded(rel_path: str, patterns: list[str]) -> bool:
    """True if a configured exclude glob matches the path, or any folder above it
    (so "HR" and "HR/*" both exclude everything under HR)."""
    if not patterns:
        return False
    parts = PurePosixPath(rel_path).parts
    candidates = [rel_path] + ["/".join(parts[:i]) for i in range(1, len(parts))]
    return any(
        fnmatch.fnmatchcase(c.lower(), p.strip("/").lower()) for c in candidates for p in patterns
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(_HASH_CHUNK):
            digest.update(block)
    return digest.hexdigest()


# ------------------------------------------------------------- configuration


def sync_sources(engine: Engine, shares: list[ShareConfig]) -> None:
    """Make the sources table match the configuration. Shares that are no longer
    configured become inactive; their documents are kept until an administrator
    removes the source (a typo in .env must not wipe an index)."""
    with engine.begin() as conn:
        for share in shares:
            stmt = pg_insert(sources).values(
                key=share.key, name=share.name, root=share.root, display_path=share.display_path
            )
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=[sources.c.key],
                    set_={
                        "name": stmt.excluded.name,
                        "root": stmt.excluded.root,
                        "display_path": stmt.excluded.display_path,
                        "active": True,
                    },
                )
            )
        keys = [s.key for s in shares]
        conn.execute(
            update(sources)
            .where(sources.c.key.not_in(keys) if keys else sources.c.id.is_not(None))
            .values(active=False)
        )


def ensure_groups(conn: Any, names: list[str]) -> list[int]:
    ids = []
    for name in names:
        clean = " ".join(name.split())[:100]
        if not clean:
            continue
        conn.execute(pg_insert(groups).values(name=clean).on_conflict_do_nothing())
        ids.append(conn.execute(select(groups.c.id).where(groups.c.name == clean)).scalar_one())
    return sorted(set(ids))


def _apply_groups(conn: Any, source_id: int, group_ids: list[int]) -> None:
    """Every document of the share is shared with exactly these groups."""
    conn.execute(
        text(
            "DELETE FROM document_groups dg USING documents d "
            "WHERE dg.document_id = d.id AND d.source_id = :sid "
            "AND NOT (dg.group_id = ANY(:gids))"
        ),
        {"sid": source_id, "gids": group_ids},
    )
    if group_ids:
        conn.execute(
            text(
                "INSERT INTO document_groups (document_id, group_id) "
                "SELECT d.id, g FROM documents d CROSS JOIN unnest(CAST(:gids AS bigint[])) g "
                "WHERE d.source_id = :sid ON CONFLICT DO NOTHING"
            ),
            {"sid": source_id, "gids": group_ids},
        )


# ----------------------------------------------------------------- scanning


@dataclass(frozen=True)
class Skipped:
    path: str  # relative POSIX path; a folder for "excluded" folders
    reason: str  # unsupported | empty | too_large | excluded
    size_bytes: int | None = None


def walk(
    root: Path, exclude: list[str], max_bytes: int
) -> tuple[dict[str, os.stat_result], list[Skipped], list[str]]:
    """Supported files under root as {relative posix path: stat}, the files
    (and excluded folders) not indexed with the reason, and the errors met
    while walking. Hidden, temporary and system files aren't listed at all."""
    found: dict[str, os.stat_result] = {}
    skipped: list[Skipped] = []
    errors: list[str] = []

    def on_error(exc: OSError) -> None:
        errors.append(f"{type(exc).__name__}: {exc.strerror or exc}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error, followlinks=False):
        here = Path(dirpath)
        keep = []
        for d in sorted(dirnames):
            if skip_dir(d):
                continue
            rel_dir = (here / d).relative_to(root).as_posix()
            if excluded(rel_dir, exclude):
                skipped.append(Skipped(rel_dir + "/", "excluded"))
            else:
                keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            if skip_name(name):
                continue
            path = here / name
            rel = path.relative_to(root).as_posix()
            if excluded(rel, exclude):
                skipped.append(Skipped(rel, "excluded"))
                continue
            try:
                st = path.stat()
            except OSError as exc:
                on_error(exc)
                continue
            if path.suffix.lower().lstrip(".") not in SUPPORTED_EXTENSIONS:
                skipped.append(Skipped(rel, "unsupported", st.st_size))
            elif st.st_size == 0:
                skipped.append(Skipped(rel, "empty", 0))
            elif st.st_size > max_bytes:
                skipped.append(Skipped(rel, "too_large", st.st_size))
            else:
                found[rel] = st
    return found, skipped, errors


def not_indexable(skipped: list[Skipped]) -> int:
    """Files skipped for their type or size (the sidebar's "skipped" count);
    files excluded by configuration aren't counted."""
    return sum(1 for s in skipped if s.reason != "excluded")


def scan_source(
    engine: Engine, source: SourceRow, share: ShareConfig, settings: Settings
) -> ScanResult:
    result = ScanResult()
    root = Path(source.root)
    max_bytes = settings.max_upload_mb * 1024 * 1024
    with engine.begin() as conn:
        group_ids = ensure_groups(conn, share.groups)
        _apply_groups(conn, source.id, group_ids)
        existing = {
            r.source_path: r
            for r in conn.execute(
                select(
                    documents.c.id,
                    documents.c.source_path,
                    documents.c.size_bytes,
                    documents.c.source_mtime,
                    documents.c.sha256,
                    documents.c.status,
                ).where(
                    documents.c.source_id == source.id,
                    # Email attachments aren't files on the share: they follow
                    # their email (app.ingestion.attachments).
                    documents.c.parent_id.is_(None),
                )
            )
        }

    if not root.is_dir():
        result.error = "The share isn't mounted (folder not found). Check SHARE_PATH in .env."
        return result
    found, skipped, errors = walk(root, share.exclude, max_bytes)
    result.skipped = not_indexable(skipped)
    _record_skipped(engine, source.id, skipped)
    if not found and existing:
        result.error = (
            "The share looks empty, so nothing was removed. If it is unmounted or "
            "unreachable, reconnect it; if it really is empty, remove its files' "
            "documents by removing the share."
        )
        return result

    for rel, st in found.items():
        path = root / rel
        old = existing.get(rel)
        if old is not None and old.size_bytes == st.st_size and old.source_mtime == st.st_mtime:
            result.unchanged += 1
            continue
        try:
            sha = file_sha256(path)
        except OSError as exc:
            errors.append(f"{type(exc).__name__}: {exc.strerror or exc}")
            continue
        ext = path.suffix.lower().lstrip(".")
        with engine.begin() as conn:
            if old is None:
                try:
                    doc_id = conn.execute(
                        insert(documents)
                        .values(
                            filename=path.name[:255],
                            sha256=sha,
                            content_type=ext,
                            size_bytes=st.st_size,
                            storage_path=str(path),
                            status="pending",
                            source_id=source.id,
                            source_path=rel,
                            source_mtime=st.st_mtime,
                        )
                        .returning(documents.c.id)
                    ).scalar_one()
                except IntegrityError:  # another scanner got there first
                    continue
                if group_ids:
                    conn.execute(
                        text(
                            "INSERT INTO document_groups (document_id, group_id) "
                            "SELECT :doc, unnest(CAST(:gids AS bigint[]))"
                        ),
                        {"doc": doc_id, "gids": group_ids},
                    )
                conn.execute(insert(ingest_jobs).values(document_id=doc_id, kind="ingest"))
                result.added += 1
            elif old.sha256 == sha or old.status == "excluded":
                # Excluded by an administrator: track the file, never queue it.
                conn.execute(
                    update(documents)
                    .where(documents.c.id == old.id)
                    .values(source_mtime=st.st_mtime, size_bytes=st.st_size, sha256=sha)
                )
                if old.sha256 == sha:
                    result.unchanged += 1
                else:
                    result.changed += 1
            else:
                conn.execute(
                    update(documents)
                    .where(documents.c.id == old.id)
                    .values(
                        sha256=sha,
                        size_bytes=st.st_size,
                        source_mtime=st.st_mtime,
                        content_type=ext,
                        status="pending",
                        error=None,
                        updated_at=func.now(),
                    )
                )
                # A queued job will read the new content anyway; one already
                # running may have read the old file, so queue another.
                if old.status != "pending":
                    conn.execute(insert(ingest_jobs).values(document_id=old.id, kind="reindex"))
                result.changed += 1

    gone = [r.id for rel, r in existing.items() if rel not in found]
    if gone and errors:
        log.warning(
            "Share %s: %d document(s) not found, but parts of the share couldn't be "
            "read; nothing removed this time",
            source.key,
            len(gone),
        )
    elif gone:
        with engine.begin() as conn:
            conn.execute(delete(documents).where(documents.c.id.in_(gone)))
        result.removed = len(gone)
    if errors:
        result.error = f"{len(errors)} file(s) or folder(s) couldn't be read, e.g. {errors[0]}"
    return result


def _record_skipped(engine: Engine, source_id: int, skipped: list[Skipped]) -> None:
    """Replace the share's list of files that aren't indexed (capped, so a huge
    archive folder can't bloat the table; the count stays exact)."""
    rows = [
        {"source_id": source_id, "path": s.path, "reason": s.reason, "size_bytes": s.size_bytes}
        for s in skipped[:_MAX_SKIPPED_LISTED]
    ]
    with engine.begin() as conn:
        conn.execute(delete(source_skipped).where(source_skipped.c.source_id == source_id))
        if rows:
            conn.execute(insert(source_skipped), rows)


def _record(engine: Engine, source_id: int, result: ScanResult, files_seen: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(sources)
            .where(sources.c.id == source_id)
            .values(
                last_scan_at=func.now(),
                last_scan_error=result.error,
                scan_requested_at=None,
                files_seen=files_seen,
                files_skipped=result.skipped,
            )
        )


def scan_due(
    engine: Engine, settings: Settings, *, force: bool = False
) -> list[tuple[str, ScanResult]]:
    """Scan every active share that was asked for, or whose interval has passed.
    Returns (key, result) for the shares scanned. Holds an advisory lock so only
    one worker scans at a time."""
    configured = {s.key: s for s in settings.shares()}
    if not configured:
        return []
    done: list[tuple[str, ScanResult]] = []
    with engine.connect() as lock_conn:
        if not lock_conn.execute(select(func.pg_try_advisory_lock(_SCAN_LOCK_ID))).scalar():
            return []
        try:
            stmt = select(sources.c.id, sources.c.key, sources.c.name, sources.c.root).where(
                sources.c.active, sources.c.key.in_(list(configured))
            )
            if not force:
                interval = func.make_interval(0, 0, 0, 0, 0, 0, settings.share_scan_interval_s)
                stmt = stmt.where(
                    sources.c.last_scan_at.is_(None)
                    | sources.c.scan_requested_at.is_not(None)
                    | (sources.c.last_scan_at < func.now() - interval)
                )
            with engine.connect() as conn:
                rows = conn.execute(stmt.order_by(sources.c.id)).all()
            for row in rows:
                src = SourceRow(row.id, row.key, row.name, row.root)
                try:
                    result = scan_source(engine, src, configured[src.key], settings)
                except Exception as exc:  # keep the worker alive; show the error
                    log.exception("Share %s: scan failed", src.key)
                    result = ScanResult(error=f"Scan failed: {exc}")
                seen = result.added + result.changed + result.unchanged
                _record(engine, src.id, result, seen)
                log.info(
                    "Share %s scanned: %d new, %d changed, %d removed, %d unchanged, %d skipped%s",
                    src.key,
                    result.added,
                    result.changed,
                    result.removed,
                    result.unchanged,
                    result.skipped,
                    f" ({result.error})" if result.error else "",
                )
                done.append((src.key, result))
        finally:
            lock_conn.execute(select(func.pg_advisory_unlock(_SCAN_LOCK_ID)))
            lock_conn.commit()
    return done
