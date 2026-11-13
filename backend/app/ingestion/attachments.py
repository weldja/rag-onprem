"""Email attachments as documents of their own.

When an email (.msg) is indexed, each supported attachment is saved in the
uploads volume (attachments/<sha256>.<ext>, shared by identical attachments)
and becomes a child document (documents.parent_id = the email), queued for
indexing like any upload. An email attached to an email is saved as a .msg
and its own attachments become its children when it is indexed.

Children follow their email:
- visibility: the email's groups (share emails: the share's groups, re-applied
  on every scan because children carry the share's source_id)
- lifecycle: re-indexing the email updates, adds and removes them; deleting or
  excluding the email does the same to them. They're re-read only when the
  email changes (share scans compare size and modification time first).
- duplicates: an attachment identical to another document copies its chunks
  (copy_from_duplicate) instead of being embedded again.
Unsupported, inline (signature images) and oversized attachments are skipped
with a log line that names the attachment's position, never its content.
"""

import hashlib
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

from sqlalchemy import Engine, delete, insert, select, text, update

from app.core.config import Settings
from app.db.tables import documents, ingest_jobs
from app.ingestion.loaders import SUPPORTED_EXTENSIONS
from app.ingestion.msg import Email
from app.ingestion.storage import upload_root

log = logging.getLogger(__name__)

SEPARATOR = " › "  # share path of an attachment: "<email path> › <attachment name>"


@dataclass(frozen=True)
class Planned:
    key: str
    filename: str
    ext: str
    sha256: str
    size: int
    path: str


def _save(folder: Path, sha: str, ext: str, data: bytes) -> Path:
    """Content-addressed, written atomically (two workers may save the same file)."""
    target = folder / f"{sha}.{ext}"
    if not target.exists():
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".part-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return target


def plan(doc_id: int, mail: Email, settings: Settings) -> list[Planned]:
    """Save the email's indexable attachments; returns what its children should be."""
    folder = upload_root(settings.upload_dir) / "attachments"
    folder.mkdir(parents=True, exist_ok=True)
    max_bytes = settings.max_upload_mb * 1024 * 1024
    out: list[Planned] = []
    for a in mail.attachments:
        ext = "msg" if a.is_message else PurePath(a.filename).suffix.lower().lstrip(".")
        reason = None
        if a.inline:
            reason = "inline image"
        elif ext not in SUPPORTED_EXTENSIONS:
            reason = f"unsupported type .{ext}" if ext else "no file extension"
        elif not a.data:
            reason = "empty"
        elif len(a.data) > max_bytes:
            reason = "too large"
        if reason or a.data is None:
            log.info("Document %d: attachment %s skipped (%s)", doc_id, a.key, reason)
            continue
        sha = hashlib.sha256(a.data).hexdigest()
        path = _save(folder, sha, ext, a.data)
        out.append(Planned(a.key, a.filename, ext, sha, len(a.data), str(path)))
    return out


def _unique_names(planned: list[Planned]) -> dict[str, str]:
    """key -> display name, numbering repeated names ("a.pdf", "a (2).pdf")."""
    seen: dict[str, int] = {}
    names: dict[str, str] = {}
    for p in planned:
        n = seen[p.filename.lower()] = seen.get(p.filename.lower(), 0) + 1
        stem, suffix = PurePath(p.filename).stem, PurePath(p.filename).suffix
        names[p.key] = p.filename if n == 1 else f"{stem} ({n}){suffix}"
    return names


def descendant_paths(conn: Any, doc_id: int) -> list[str]:
    """Stored files of a document's attachments, at any depth."""
    return list(
        conn.execute(
            text(
                """
                WITH RECURSIVE tree(id) AS (
                    SELECT id FROM documents WHERE parent_id = :doc
                    UNION ALL
                    SELECT d.id FROM documents d JOIN tree t ON d.parent_id = t.id)
                SELECT d.storage_path FROM documents d JOIN tree t ON d.id = t.id
                 WHERE d.storage_path IS NOT NULL
                """
            ),
            {"doc": doc_id},
        ).scalars()
    )


def sync(conn: Any, parent_id: int, planned: list[Planned]) -> tuple[int, int, list[str]]:
    """Make the email's child documents match `planned`, in the caller's
    transaction. Returns (queued, removed, storage paths no longer used by
    those children: pass them to discard_unreferenced after commit)."""
    parent = conn.execute(
        select(documents.c.source_id, documents.c.source_path, documents.c.uploaded_by).where(
            documents.c.id == parent_id
        )
    ).one()
    existing = {
        r.attachment_key: r
        for r in conn.execute(
            select(
                documents.c.id,
                documents.c.attachment_key,
                documents.c.sha256,
                documents.c.status,
                documents.c.storage_path,
            ).where(documents.c.parent_id == parent_id)
        )
    }
    wanted = {p.key: p for p in planned}
    names = _unique_names(planned)
    released: list[str] = []

    gone = [r.id for key, r in existing.items() if key not in wanted]
    for doc in gone:
        released += descendant_paths(conn, doc)
    released += [r.storage_path for key, r in existing.items() if key not in wanted]
    if gone:
        conn.execute(delete(documents).where(documents.c.id.in_(gone)))
    if parent.source_id is not None and existing:
        # Free the share paths first, so renamed or reordered attachments
        # can't collide with each other's old names.
        conn.execute(
            update(documents)
            .where(documents.c.parent_id == parent_id)
            .values(source_path=documents.c.source_path + "\x01" + documents.c.attachment_key)
        )

    queued = 0
    for key, p in wanted.items():
        share_path = (
            f"{parent.source_path}{SEPARATOR}{names[key]}" if parent.source_id is not None else None
        )
        old = existing.get(key)
        values: dict[str, Any] = {
            "filename": names[key][:255],
            "content_type": p.ext,
            "size_bytes": p.size,
            "storage_path": p.path,
            "sha256": p.sha256,
            "source_path": share_path,
        }
        if old is None:
            child = conn.execute(
                insert(documents)
                .values(
                    **values,
                    status="pending",
                    uploaded_by=parent.uploaded_by,
                    source_id=parent.source_id,
                    parent_id=parent_id,
                    attachment_key=key,
                )
                .returning(documents.c.id)
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO document_groups (document_id, group_id) "
                    "SELECT :child, group_id FROM document_groups WHERE document_id = :parent"
                ),
                {"child": child, "parent": parent_id},
            )
            conn.execute(insert(ingest_jobs).values(document_id=child, kind="ingest"))
            queued += 1
            continue
        changed = old.sha256 != p.sha256
        if changed and old.status != "excluded":
            values["status"] = "pending"
            values["error"] = None
        conn.execute(update(documents).where(documents.c.id == old.id).values(**values))
        if changed:
            if old.storage_path != p.path:
                released.append(old.storage_path)
            if old.status not in ("excluded", "pending"):
                conn.execute(insert(ingest_jobs).values(document_id=old.id, kind="reindex"))
                queued += 1
    return queued, len(gone), [r for r in released if r]


def discard_unreferenced(engine: Engine, paths: list[str], upload_dir: str) -> None:
    """Delete stored attachment files no document uses any more (identical
    attachments of several emails share one file). Only files in the
    attachments folder are ever deleted: never uploads or files on a share."""
    folder = (upload_root(upload_dir) / "attachments").resolve()
    for path in set(paths):
        if Path(path).resolve().parent != folder:
            continue
        with engine.connect() as conn:
            used = conn.execute(
                select(documents.c.id).where(documents.c.storage_path == path).limit(1)
            ).first()
        if used is None:
            Path(path).unlink(missing_ok=True)
