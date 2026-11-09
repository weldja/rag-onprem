"""File-share discovery rules (no database; see tests/db/test_shares.py)."""

from pathlib import Path

from app.core.config import Settings
from app.ingestion.shares import excluded, not_indexable, skip_dir, skip_name, walk


def test_lock_temp_and_system_files_are_skipped() -> None:
    for name in [
        "~$report.docx",
        "~WRL0001.tmp",
        ".~lock.sheet.xlsx#",
        ".hidden.pdf",
        "Thumbs.db",
        "desktop.ini",
        "download.pdf.crdownload",
        "x.partial",
    ]:
        assert skip_name(name), name
    for name in ["report.docx", "Q3 ~ draft.pdf", "notes.md"]:
        assert not skip_name(name), name
    assert skip_dir("$RECYCLE.BIN") and skip_dir(".snapshot") and skip_dir("#recycle")
    assert not skip_dir("Policies")


def test_exclude_globs_match_the_path_or_a_folder_above_it() -> None:
    assert excluded("HR/pay/2026.xlsx", ["HR"])
    assert excluded("HR/pay/2026.xlsx", ["hr/*"])
    assert excluded("Archive/old.pdf", ["*.tmp", "archive/*"])
    assert excluded("drafts/x.docx", ["*/x.docx"])
    assert not excluded("Policies/HR-summary.pdf", ["HR", "HR/*"])
    assert not excluded("anything.pdf", [])


def test_walk_finds_supported_files_and_counts_the_rest(tmp_path: Path) -> None:
    (tmp_path / "Policies" / "HR").mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    (tmp_path / "Policies" / "leave.pdf").write_bytes(b"%PDF")
    (tmp_path / "Policies" / "~$leave.docx").write_bytes(b"lock")
    (tmp_path / "Policies" / "HR" / "pay.xlsx").write_bytes(b"x")
    (tmp_path / "Policies" / "tool.exe").write_bytes(b"x")  # unsupported
    (tmp_path / "Policies" / "empty.txt").write_bytes(b"")  # empty
    (tmp_path / "Policies" / "big.txt").write_bytes(b"x" * 2000)  # over the limit
    (tmp_path / ".git" / "notes.md").write_bytes(b"x")
    (tmp_path / "Policies" / "draft.docx").write_bytes(b"x")
    found, skipped, errors = walk(tmp_path, ["Policies/HR", "*/draft.docx"], max_bytes=1000)
    assert sorted(found) == ["Policies/leave.pdf"]
    assert sorted((s.path, s.reason) for s in skipped) == [
        ("Policies/HR/", "excluded"),
        ("Policies/big.txt", "too_large"),
        ("Policies/draft.docx", "excluded"),
        ("Policies/empty.txt", "empty"),
        ("Policies/tool.exe", "unsupported"),
    ]
    assert not_indexable(skipped) == 3
    assert errors == []


def test_shares_from_settings() -> None:
    s = Settings(
        share_1_path="/mnt/policies",
        share_1_groups="All staff, Managers",
        share_1_exclude="HR/*",
        share_3_path="C:\\Shares\\Client Files\\",
        share_3_display_path="\\\\fs01\\clients",
        share_3_name="",
    )
    one, three = s.shares()
    assert (one.key, one.name, one.root, one.display_path) == (
        "share1",
        "policies",
        "/shares/1",
        "/mnt/policies",
    )
    assert one.groups == ["All staff", "Managers"] and one.exclude == ["HR/*"]
    assert (three.key, three.name, three.root) == ("share3", "Client Files", "/shares/3")
    assert three.display_path == "\\\\fs01\\clients"
    assert Settings().shares() == []
