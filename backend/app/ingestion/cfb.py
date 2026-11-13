"""Minimal writer for Compound File Binary files (MS-CFB, version 3), the
container format of Outlook .msg files. Reading is done with olefile.

Used to save an email attached to another email as a .msg file of its own, so
it is indexed (and downloadable) like any other document. olefile and Outlook
read the result; nothing else about the format is needed here.
"""

import struct
from collections.abc import Mapping

SECTOR = 512
MINI_SECTOR = 64
MINI_CUTOFF = 4096
FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD
DIFSECT = 0xFFFFFFFC
NOSTREAM = 0xFFFFFFFF
_HEADER_DIFAT = 109
_PER_SECTOR = SECTOR // 4  # 128 sector numbers per FAT or DIFAT sector


class _Entry:
    def __init__(self, name: str, kind: int, data: bytes = b"") -> None:
        self.name = name
        self.kind = kind  # 1 storage, 2 stream, 5 root
        self.data = data
        self.children: list[_Entry] = []
        self.index = 0
        self.left = self.right = self.child = NOSTREAM
        self.start = ENDOFCHAIN
        self.size = 0


def _sort_key(name: str) -> tuple[int, str]:
    # MS-CFB 2.6.4: shorter names first, then case-insensitive comparison.
    return (len(name.encode("utf-16-le")) // 2, name.upper())


def _tree(entries: list[_Entry]) -> int:
    """Siblings as a balanced binary search tree; returns the subtree root's
    index. All nodes are black: readers don't check red-black properties."""
    if not entries:
        return NOSTREAM
    mid = len(entries) // 2
    node = entries[mid]
    node.left = _tree(entries[:mid])
    node.right = _tree(entries[mid + 1 :])
    return node.index


def _chain(fat: list[int], start: int, count: int) -> int:
    if count == 0:
        return ENDOFCHAIN
    for i in range(start, start + count - 1):
        fat[i] = i + 1
    fat[start + count - 1] = ENDOFCHAIN
    return start


def write_cfb(streams: Mapping[str, bytes]) -> bytes:
    """A compound file holding `streams`, keyed by "/"-separated paths
    ("__substg1.0_0037001F", "__attach_version1.0_#00000000/__substg1.0_37010102").
    Storages are created for every path prefix."""
    root = _Entry("Root Entry", 5)
    storages: dict[str, _Entry] = {"": root}
    for path in sorted(streams):
        parts = path.split("/")
        parent = root
        for depth in range(len(parts) - 1):
            key = "/".join(parts[: depth + 1])
            if key not in storages:
                storages[key] = _Entry(parts[depth], 1)
                parent.children.append(storages[key])
            parent = storages[key]
        parent.children.append(_Entry(parts[-1], 2, streams[path]))

    ordered: list[_Entry] = []

    def visit(entry: _Entry) -> None:
        entry.index = len(ordered)
        ordered.append(entry)
        entry.children.sort(key=lambda e: _sort_key(e.name))
        for c in entry.children:
            visit(c)

    visit(root)
    for e in ordered:
        e.child = _tree(e.children)

    # Small streams live in the mini stream (64-byte sectors), the rest in
    # regular sectors.
    mini = bytearray()
    minifat: list[int] = []
    big = [e for e in ordered if e.kind == 2 and len(e.data) >= MINI_CUTOFF]
    for e in ordered:
        if e.kind != 2:
            continue
        e.size = len(e.data)
        if e.size == 0 or e.size >= MINI_CUTOFF:
            continue
        n = -(-e.size // MINI_SECTOR)
        e.start = len(minifat)
        minifat.extend(range(e.start + 1, e.start + n))
        minifat.append(ENDOFCHAIN)
        mini += e.data + b"\0" * (n * MINI_SECTOR - e.size)

    def sectors(n_bytes: int) -> int:
        return -(-n_bytes // SECTOR)

    n_dir = sectors(len(ordered) * 128)
    n_minifat = sectors(len(minifat) * 4)
    n_mini = sectors(len(mini))
    n_big = sum(sectors(len(e.data)) for e in big)
    data_sectors = n_dir + n_minifat + n_mini + n_big
    n_fat = n_difat = 0
    while True:  # FAT and DIFAT sectors also need FAT entries
        total = data_sectors + n_fat + n_difat
        fat_needed = -(-total // _PER_SECTOR)
        difat_needed = max(0, -(-(fat_needed - _HEADER_DIFAT) // (_PER_SECTOR - 1)))
        if (fat_needed, difat_needed) == (n_fat, n_difat):
            break
        n_fat, n_difat = fat_needed, difat_needed
    total = data_sectors + n_fat + n_difat

    fat = [FREESECT] * (n_fat * _PER_SECTOR)
    pos = 0
    fat_sectors = list(range(pos, pos + n_fat))
    for s in fat_sectors:
        fat[s] = FATSECT
    pos += n_fat
    difat_sectors = list(range(pos, pos + n_difat))
    for s in difat_sectors:
        fat[s] = DIFSECT
    pos += n_difat
    dir_start = _chain(fat, pos, n_dir)
    pos += n_dir
    minifat_start = _chain(fat, pos, n_minifat)
    pos += n_minifat
    root.start = _chain(fat, pos, n_mini)
    root.size = len(mini)
    pos += n_mini
    for e in big:
        n = sectors(len(e.data))
        e.start = _chain(fat, pos, n)
        pos += n
    assert pos == total

    body = bytearray(total * SECTOR)

    def put(sector: int, data: bytes) -> None:
        body[sector * SECTOR : sector * SECTOR + len(data)] = data

    for i, s in enumerate(fat_sectors):
        put(s, struct.pack(f"<{_PER_SECTOR}I", *fat[i * _PER_SECTOR : (i + 1) * _PER_SECTOR]))
    extra = fat_sectors[_HEADER_DIFAT:]
    for i, s in enumerate(difat_sectors):
        ids = extra[i * (_PER_SECTOR - 1) : (i + 1) * (_PER_SECTOR - 1)]
        ids += [FREESECT] * (_PER_SECTOR - 1 - len(ids))
        nxt = difat_sectors[i + 1] if i + 1 < len(difat_sectors) else ENDOFCHAIN
        put(s, struct.pack(f"<{_PER_SECTOR}I", *ids, nxt))

    directory = bytearray()
    for e in ordered:
        name = e.name.encode("utf-16-le")[:62] + b"\0\0"
        directory += struct.pack(
            "<64sHBBIII16sIQQIQ",
            name,
            len(name),
            e.kind,
            1,  # black
            e.left,
            e.right,
            e.child,
            b"\0" * 16,
            0,
            0,
            0,
            e.start if (e.kind == 5 or e.size) else ENDOFCHAIN,
            e.size,
        )
    unused = struct.pack("<64sHBBIII16sIQQIQ", b"", 0, 0, 0, NOSTREAM, NOSTREAM, NOSTREAM,
                         b"\0" * 16, 0, 0, 0, 0, 0)  # fmt: skip
    while len(directory) < n_dir * SECTOR:
        directory += unused
    put(dir_start, bytes(directory))
    if n_minifat:
        table = minifat + [FREESECT] * (n_minifat * _PER_SECTOR - len(minifat))
        put(minifat_start, struct.pack(f"<{len(table)}I", *table))
    if n_mini:
        put(root.start, bytes(mini))
    for e in big:
        put(e.start, e.data)

    header_difat = fat_sectors[:_HEADER_DIFAT] + [FREESECT] * (
        _HEADER_DIFAT - min(n_fat, _HEADER_DIFAT)
    )
    header = struct.pack(
        "<8s16sHHHHH6sIIIIIIIII",
        bytes.fromhex("D0CF11E0A1B11AE1"),
        b"\0" * 16,
        0x003E,  # minor version
        0x0003,  # major version: 512-byte sectors
        0xFFFE,  # little-endian
        9,  # sector shift
        6,  # mini sector shift
        b"\0" * 6,
        0,  # directory sectors (0 for version 3)
        n_fat,
        dir_start,
        0,  # transaction signature
        MINI_CUTOFF,
        minifat_start,
        n_minifat,
        difat_sectors[0] if difat_sectors else ENDOFCHAIN,
        n_difat,
    )
    header += struct.pack(f"<{_HEADER_DIFAT}I", *header_difat)
    return header + bytes(body)
