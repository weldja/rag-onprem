import datetime as dt
from pathlib import Path

import pytest

from app.core.config import Settings
from app.ingestion.chunking import Document
from app.ingestion.loaders import (
    LoadError,
    detect_header_row,
    is_toc_chunk,
    load_file,
    load_pdf,
    sheet_to_documents,
)
from app.ingestion.pipeline import split
from tests.pdfgen import SAMPLE_HANDBOOK, make_pdf

# ---------------------------------------------------------------- PDF


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


# --------------------------------------------------------------- DOCX


def test_docx_groups_by_heading_and_keeps_tables_in_order(tmp_path: Path) -> None:
    import docx

    d = docx.Document()
    d.add_paragraph("Intro text before any heading.")
    d.add_heading("Expenses", level=1)
    d.add_paragraph("Mileage is paid per mile.")
    table = d.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Band", "Rate"
    table.cell(1, 0).text, table.cell(1, 1).text = "First 10,000", "45p"
    d.add_heading("Security", level=1)
    d.add_paragraph("Use MFA.")
    path = tmp_path / "policy.docx"
    d.save(str(path))

    pages = load_file(path, "docx").pages
    assert [p.metadata.get("section") for p in pages] == [None, "Expenses", "Security"]
    assert (
        pages[1].page_content
        == "Expenses\nMileage is paid per mile.\nBand | Rate\nFirst 10,000 | 45p"
    )


def test_docx_merged_cells_are_read_once(tmp_path: Path) -> None:
    # The shape of a change-request form: a title merged across the row, and
    # a value merged down two rows.
    import docx

    d = docx.Document()
    table = d.add_table(rows=3, cols=4)
    table.cell(0, 0).merge(table.cell(0, 3)).text = "Change Request Summary"
    table.cell(1, 0).text = "Reason"
    table.cell(1, 1).merge(table.cell(2, 3)).text = "Too many unhandled traps"
    table.cell(2, 0).text = "Priority"
    path = tmp_path / "form.docx"
    d.save(str(path))

    assert load_file(path, "docx").pages[0].page_content == (
        "Change Request Summary\nReason | Too many unhandled traps\nPriority"
    )


def test_embedding_text_names_the_document_and_section() -> None:
    from app.ingestion.pipeline import embedding_text

    body = Document(page_content="Priority: 1, High", metadata={"section": "Summary"})
    assert embedding_text("ETHACR245- EMS_trap assessment.docx", body) == (
        "ETHACR245- EMS trap assessment - Summary\n\nPriority: 1, High"
    )
    starts = Document(page_content="Summary\nPriority", metadata={"section": "Summary"})
    assert embedding_text("form.docx", starts) == "form\n\nSummary\nPriority"
    assert embedding_text("a.pdf", Document(page_content="x", metadata={})) == "a\n\nx"


# --------------------------------------------------------------- PPTX


def test_pptx_one_piece_per_slide_with_notes(tmp_path: Path) -> None:
    from pptx import Presentation

    prs = Presentation()
    for title, notes in [("Q3 results", "Revenue up 4%"), ("", ""), ("Roadmap", "")]:
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        if title:
            slide.shapes.title.text = title
        if notes:
            slide.notes_slide.notes_text_frame.text = notes
    path = tmp_path / "deck.pptx"
    prs.save(str(path))

    loaded = load_file(path, "pptx")
    assert loaded.page_count == 3
    assert [p.metadata["page"] for p in loaded.pages] == [1, 3]
    assert loaded.pages[0].page_content == "Q3 results\nNotes: Revenue up 4%"


# -------------------------------------------------------- spreadsheets


def test_header_detection_skips_title_banner() -> None:
    rows = [
        ["Rate card 2026", "", ""],
        ["Prepared by finance", "", ""],
        ["Role", "Day rate", "Currency"],
        ["Engineer", "650", "GBP"],
    ]
    idx, preamble = detect_header_row(rows)
    assert idx == 2
    assert preamble == "Rate card 2026\nPrepared by finance"


def test_sheet_chunks_repeat_header_and_record_rows() -> None:
    rows = [["Role", "Rate"]] + [[f"Role {i}", str(i)] for i in range(1, 31)]
    docs = sheet_to_documents(rows, "Rates", max_chars=1800)
    assert len(docs) == 2  # 25 rows per chunk
    assert all(d.page_content.startswith("Sheet Rates columns: Role | Rate") for d in docs)
    assert docs[0].metadata == {"section": "Sheet Rates, rows 2-26", "pre_chunked": True}
    assert docs[1].metadata["section"] == "Sheet Rates, rows 27-31"


def test_xlsx_reads_all_sheets_and_formats_values(tmp_path: Path) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Staff"
    ws.append(["Name", "Start date", "Days"])
    ws.append(["Ada", dt.datetime(2024, 3, 1), 27.0])
    other = wb.create_sheet("Notes")
    other.append(["Topic", "Detail"])
    other.append(["Carry-over", "Up to 5 days"])
    path = tmp_path / "staff.xlsx"
    wb.save(str(path))

    pages = load_file(path, "xlsx").pages
    assert [p.metadata["section"] for p in pages] == [
        "Sheet Staff, rows 2-2",
        "Sheet Notes, rows 2-2",
    ]
    assert "Ada | 2024-03-01 | 27" in pages[0].page_content


def test_csv_with_semicolons(tmp_path: Path) -> None:
    path = tmp_path / "rates.csv"
    path.write_text("Band;Rate\nFirst 10000;45p\nAfter;25p\n", encoding="utf-8")
    pages = load_file(path, "csv").pages
    assert len(pages) == 1
    assert pages[0].page_content == "Table columns: Band | Rate\nFirst 10000 | 45p\nAfter | 25p"


# -------------------------------------------------------- HTML / text


def test_html_drops_scripts_and_uses_title(tmp_path: Path) -> None:
    path = tmp_path / "page.html"
    path.write_text(
        "<html><head><title>VPN guide</title><script>evil()</script></head>"
        "<body><h1>Connect</h1><p>Open the client.</p></body></html>",
        encoding="utf-8",
    )
    pages = load_file(path, "html").pages
    assert pages[0].metadata == {"section": "VPN guide"}
    assert "Open the client." in pages[0].page_content
    assert "evil" not in pages[0].page_content


def test_text_and_unsupported(tmp_path: Path) -> None:
    path = tmp_path / "notes.md"
    path.write_bytes(b"\xef\xbb\xbf# Notes\nHello\x00 world")
    assert load_file(path, "md").pages[0].page_content == "# Notes\nHello world"
    with pytest.raises(LoadError):
        load_file(path, "exe")


# ---------------------------------------------------------- TOC + split


def test_toc_detection() -> None:
    toc = "Contents\n1. Leave ........ 2\n2. Expenses ........ 3\n3. IT ....... 4"
    assert is_toc_chunk(toc, page=1)
    assert not is_toc_chunk("Fees are set out in clause 4.2 and total 1,200", page=1)


def test_split_keeps_pre_chunked_and_drops_toc() -> None:
    settings = Settings(chunk_size=50, chunk_overlap=0)
    pieces = [
        Document("Contents\nIntro ........ 1\nLeave ........ 2", {"page": 1}),
        Document("word " * 30, {"page": 2}),
        Document("x" * 120, {"section": "Sheet A, rows 2-3", "pre_chunked": True}),
    ]
    out = split(pieces, settings)
    assert all("Contents" not in c.page_content for c in out)
    assert any(len(c.page_content) == 120 for c in out)  # sheet chunk not re-split
    assert len([c for c in out if c.metadata.get("page") == 2]) > 1
