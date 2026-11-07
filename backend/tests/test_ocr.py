"""OCR of scanned PDFs and images. Needs the tesseract binary, which is in the
backend images; skipped elsewhere."""

from pathlib import Path

import pytest

from app.ingestion.loaders import LoadError, load_file
from app.ingestion.ocr import OcrOptions, tesseract_available

pytestmark = pytest.mark.skipif(not tesseract_available(), reason="tesseract not installed")

ON = OcrOptions()
VISITORS = [
    "Visitor policy",
    "Visitors must sign in at reception",
    "and wear a badge at all times.",
]


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def test_scanned_pdf_is_read_with_ocr(tmp_path: Path) -> None:
    from tests.scanned import scanned_pdf

    pdf = _write(
        tmp_path,
        "scan.pdf",
        scanned_pdf([VISITORS, ["Parking", "Spaces are allocated by ballot."]]),
    )
    loaded = load_file(pdf, "pdf", ocr=ON)
    assert loaded.page_count == 2
    assert [p.metadata for p in loaded.pages] == [
        {"page": 1, "ocr": True},
        {"page": 2, "ocr": True},
    ]
    assert "sign in at reception" in _norm(loaded.pages[0].page_content)
    assert "ballot" in _norm(loaded.pages[1].page_content)


def test_only_pages_without_text_are_ocrd(tmp_path: Path) -> None:
    from tests.scanned import mixed_pdf

    text_page = ["Typed page: annual leave is 27 days a year for full-time staff."]
    pdf = _write(tmp_path, "mixed.pdf", mixed_pdf(text_page, VISITORS))
    pages = load_file(pdf, "pdf", ocr=ON).pages
    assert pages[0].metadata == {"page": 1}  # text layer used as-is
    assert pages[1].metadata == {"page": 2, "ocr": True}
    assert "badge" in _norm(pages[1].page_content)


def test_ocr_disabled_leaves_scans_unread(tmp_path: Path) -> None:
    from tests.scanned import scanned_pdf

    pdf = _write(tmp_path, "scan.pdf", scanned_pdf([VISITORS]))
    assert load_file(pdf, "pdf", ocr=OcrOptions(enabled=False)).pages == []


def test_ocr_page_limit(tmp_path: Path) -> None:
    from tests.scanned import scanned_pdf

    pdf = _write(tmp_path, "scan.pdf", scanned_pdf([VISITORS, VISITORS, VISITORS]))
    loaded = load_file(pdf, "pdf", ocr=OcrOptions(max_pages=1))
    assert [p.metadata["page"] for p in loaded.pages] == [1]
    assert loaded.page_count == 3


def test_png_and_multipage_tiff(tmp_path: Path) -> None:
    from tests.scanned import png, tiff

    one = load_file(_write(tmp_path, "notice.png", png(VISITORS)), "png", ocr=ON)
    assert "reception" in _norm(one.pages[0].page_content)
    many = load_file(
        _write(tmp_path, "fax.tiff", tiff([VISITORS, ["Second page of the fax."]])), "tiff", ocr=ON
    )
    assert many.page_count == 2
    assert [p.metadata["page"] for p in many.pages] == [1, 2]
    assert "second page" in _norm(many.pages[1].page_content)


def test_images_need_ocr(tmp_path: Path) -> None:
    from tests.scanned import png

    with pytest.raises(LoadError, match="OCR is disabled"):
        load_file(_write(tmp_path, "a.png", png(VISITORS)), "png", ocr=OcrOptions(enabled=False))


def test_unknown_language_is_a_clear_error(tmp_path: Path) -> None:
    from tests.scanned import png

    with pytest.raises(LoadError, match="Tesseract failed"):
        load_file(_write(tmp_path, "a.png", png(VISITORS)), "png", ocr=OcrOptions(languages="xyz"))
