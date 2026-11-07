from pathlib import Path

import pytest

from app.ingestion.loaders import LoadError, load_pdf
from tests.pdfgen import SAMPLE_HANDBOOK, make_pdf


def test_load_pdf_numbers_pages_from_one(tmp_path: Path) -> None:
    pdf = tmp_path / "handbook.pdf"
    pdf.write_bytes(make_pdf(SAMPLE_HANDBOOK))
    loaded = load_pdf(pdf)
    assert loaded.page_count == 4
    assert [d.metadata["page"] for d in loaded.pages] == [1, 2, 3, 4]
    assert "27 days of annual leave" in loaded.pages[1].page_content


def test_blank_pages_are_skipped_but_counted(tmp_path: Path) -> None:
    pdf = tmp_path / "gap.pdf"
    pdf.write_bytes(make_pdf([["first page"], [], ["third page (with parens)"]]))
    loaded = load_pdf(pdf)
    assert loaded.page_count == 3
    assert [d.metadata["page"] for d in loaded.pages] == [1, 3]
    assert "(with parens)" in loaded.pages[1].page_content


def test_not_a_pdf_raises_load_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"this is not a pdf")
    with pytest.raises(LoadError):
        load_pdf(bad)
