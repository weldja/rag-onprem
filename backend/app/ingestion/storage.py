"""Original files are kept in the uploads volume as <sha256>.<ext>, so they can
be re-indexed later (e.g. after an embedding-model change)."""

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

_CHUNK = 1024 * 1024


class TooLargeError(ValueError):
    pass


@dataclass
class StoredFile:
    path: Path  # temporary file inside the upload dir
    sha256: str
    size: int


def upload_root(upload_dir: str) -> Path:
    root = Path(upload_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def receive(src: BinaryIO, upload_dir: str, max_bytes: int) -> StoredFile:
    """Copy a stream to a temp file in the upload dir, hashing as it goes.
    The temp file lives on the same filesystem so finalise() is an atomic rename."""
    root = upload_root(upload_dir)
    digest = hashlib.sha256()
    size = 0
    fd, tmp_name = tempfile.mkstemp(dir=root, prefix=".incoming-")
    try:
        with os.fdopen(fd, "wb") as out:
            while block := src.read(_CHUNK):
                size += len(block)
                if size > max_bytes:
                    raise TooLargeError(f"File exceeds the {max_bytes // (1024 * 1024)} MB limit")
                digest.update(block)
                out.write(block)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return StoredFile(Path(tmp_name), digest.hexdigest(), size)


def receive_path(path: Path, upload_dir: str, max_bytes: int) -> StoredFile:
    with path.open("rb") as fh:
        return receive(fh, upload_dir, max_bytes)


def finalise(stored: StoredFile, upload_dir: str, ext: str) -> Path:
    target = upload_root(upload_dir) / f"{stored.sha256}.{ext}"
    if target.exists():
        stored.path.unlink(missing_ok=True)
    else:
        shutil.move(stored.path, target)
    return target


def discard(path: str | Path | None) -> None:
    if path:
        Path(path).unlink(missing_ok=True)
