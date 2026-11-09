"""Document loaders: file -> list of Documents with page/section metadata.

Metadata keys:
  page         1-based page (PDF, image frame) or slide (PPTX) number
  ocr          True if the text came from OCR (scanned page or image)
  section      heading (DOCX), sheet name and row range (XLSX/CSV)
  pre_chunked  True if the loader already produced chunk-sized pieces (spreadsheets),
               so the splitter must leave them alone

Spreadsheet header detection and row grouping are ported from rag-claude's
_load_spreadsheet, without the pandas dependency.
"""

import csv
import datetime as dt
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.ingestion.chunking import Document
from app.ingestion.ocr import OcrError, OcrOptions, image_frames, ocr_image, render_pdf_page

log = logging.getLogger(__name__)

IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "tif", "tiff"})
SUPPORTED_EXTENSIONS = (
    frozenset({"pdf", "docx", "pptx", "xlsx", "csv", "html", "htm", "txt", "md"}) | IMAGE_EXTENSIONS
)

SHEET_MAX_ROWS_PER_CHUNK = 25


class LoadError(ValueError):
    """The file can't be read as a supported document."""


@dataclass
class LoadedDocument:
    pages: list[Document]  # pieces with extractable text, in document order
    page_count: int | None  # pages/slides for paged formats, else None


def _clean(text: str) -> str:
    # Postgres text columns reject NUL bytes, which some files produce.
    return text.replace("\x00", "").strip()


# --------------------------------------------------------------------------- PDF


def load_pdf(path: Path, ocr: OcrOptions | None = None) -> LoadedDocument:
    """Text layer per page. Pages with (almost) no text layer, i.e. scans, are
    OCR'd when `ocr` is enabled, up to ocr.max_pages per document."""
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise LoadError(f"{path.name} is encrypted")
        pages: list[Document] = []
        ocr_done = 0
        for number, page in enumerate(reader.pages, start=1):
            text = _clean(page.extract_text() or "")
            meta: dict[str, Any] = {"page": number}
            if ocr is not None and ocr.enabled and len(text) < ocr.min_chars:
                if ocr_done < ocr.max_pages:
                    scanned = _clean(ocr_image(render_pdf_page(path, number - 1, ocr.dpi), ocr))
                    if len(scanned) > len(text):
                        text, meta["ocr"] = scanned, True
                elif ocr_done == ocr.max_pages:
                    log.warning("%s: OCR limit of %d pages reached", path.name, ocr.max_pages)
                ocr_done += 1
            if text:
                pages.append(Document(page_content=text, metadata=meta))
        return LoadedDocument(pages=pages, page_count=len(reader.pages))
    except PdfReadError as exc:
        raise LoadError(f"{path.name} is not a readable PDF: {exc}") from exc
    except OcrError as exc:
        raise LoadError(f"{path.name}: {exc}") from exc


# ------------------------------------------------------------------------ images


def load_image(path: Path, ocr: OcrOptions | None) -> LoadedDocument:
    """Photos and scans (PNG, JPEG, TIFF): OCR every frame, one page per frame."""
    if ocr is None or not ocr.enabled:
        raise LoadError(f"{path.name} is an image; OCR is disabled so it can't be read")
    try:
        frames = image_frames(path)
    except Exception as exc:  # Pillow raises several unrelated types
        raise LoadError(f"{path.name} is not a readable image: {exc}") from exc
    pages: list[Document] = []
    try:
        for number, frame in enumerate(frames[: ocr.max_pages], start=1):
            text = _clean(ocr_image(frame, ocr))
            if text:
                pages.append(Document(page_content=text, metadata={"page": number, "ocr": True}))
    except OcrError as exc:
        raise LoadError(f"{path.name}: {exc}") from exc
    return LoadedDocument(pages=pages, page_count=len(frames))


# -------------------------------------------------------------------------- DOCX


def load_docx(path: Path) -> LoadedDocument:
    """Paragraphs and tables in document order, grouped under their headings."""
    import docx
    from docx.table import Table

    try:
        document = docx.Document(str(path))
    except Exception as exc:  # python-docx raises several unrelated types
        raise LoadError(f"{path.name} is not a readable DOCX: {exc}") from exc

    sections: list[Document] = []
    heading: str | None = None
    lines: list[str] = []

    def flush() -> None:
        text = _clean("\n".join(lines))
        if text:
            meta = {"section": heading} if heading else {}
            sections.append(Document(page_content=text, metadata=meta))

    for block in document.iter_inner_content():
        if isinstance(block, Table):
            # python-docx returns a merged cell once per grid column it spans and
            # again in each row it spans; keep its text only the first time. Forms
            # otherwise repeat titles four or five times and push the fields that
            # belong with them into another chunk.
            seen: set[Any] = set()  # the cell elements themselves, kept alive
            for row in block.rows:
                cells = []
                for c in row.cells:
                    if c._tc in seen:
                        continue
                    seen.add(c._tc)
                    if c.text.strip():
                        cells.append(c.text.strip())
                if cells:
                    lines.append(" | ".join(cells))
            continue
        text = block.text.strip()
        if not text:
            continue
        style = (block.style.name if block.style is not None else "") or ""
        if style.startswith("Heading") or style == "Title":
            flush()
            lines = [text]
            heading = text[:200]
        else:
            lines.append(text)
    flush()
    return LoadedDocument(pages=sections, page_count=None)


# -------------------------------------------------------------------------- PPTX


def load_pptx(path: Path) -> LoadedDocument:
    """One piece per slide, so slide boundaries survive and slides are citable."""
    from pptx import Presentation

    try:
        prs = Presentation(str(path))
    except Exception as exc:
        raise LoadError(f"{path.name} is not a readable PPTX: {exc}") from exc

    slides: list[Document] = []
    count = 0
    for number, slide in enumerate(prs.slides, start=1):
        count = number
        texts = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
                texts.append(shape.text_frame.text.strip())
            elif getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        texts.append(" | ".join(cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                texts.append(f"Notes: {notes}")
        text = _clean("\n".join(texts))
        if text:
            slides.append(Document(page_content=text, metadata={"page": number}))
    return LoadedDocument(pages=slides, page_count=count)


# ------------------------------------------------------------------ spreadsheets


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, dt.datetime):
        return value.date().isoformat() if value.time() == dt.time() else value.isoformat(" ")
    if isinstance(value, dt.date):
        return value.isoformat()
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def header_score(row: Sequence[str]) -> float:
    """How header-like a row is (0..1): mostly populated, mostly unique, mostly
    short text labels rather than sentences or numbers."""
    values = [v for v in row if v]
    if not row or not values:
        return 0.0
    non_null = len(values) / len(row)
    unique = len({v.lower() for v in values}) / len(values)
    labels = 0
    for v in values:
        try:
            float(v.replace(",", ""))
            numeric = True
        except ValueError:
            numeric = False
        if not numeric and 1 <= len(v.split()) <= 6 and len(v) <= 60:
            labels += 1
    return 0.4 * non_null + 0.3 * unique + 0.3 * (labels / len(values))


def detect_header_row(rows: Sequence[Sequence[str]], max_scan: int = 15) -> tuple[int, str]:
    """(header_row_index, preamble_text). Title banners and notes above the real
    table are common; the preamble is returned so it can be indexed too."""
    best_idx, best_score = 0, -1.0
    for i, row in enumerate(rows[:max_scan]):
        if sum(1 for v in row if v) < 2:  # a single-cell title can't be the header
            continue
        score = header_score(row)
        if score > best_score:
            best_idx, best_score = i, score
    if best_score < 0.45:
        best_idx = 0
    preamble = [" ".join(v for v in row if v) for row in rows[:best_idx]]
    return best_idx, "\n".join(line for line in preamble if line)


def clean_columns(header: Sequence[str]) -> list[str]:
    seen: dict[str, int] = {}
    cols = []
    for j, h in enumerate(header):
        name = h or f"Column {j + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name} ({seen[name]})"
        else:
            seen[name] = 0
        cols.append(name)
    return cols


def _render_row(values: Sequence[str]) -> str:
    cells = list(values)
    while cells and not cells[-1]:
        cells.pop()
    return " | ".join(cells)


def sheet_to_documents(rows: list[list[str]], sheet: str | None, max_chars: int) -> list[Document]:
    """Row groups with the column header repeated on every chunk (a row group
    without its header is unanswerable). Marked pre_chunked."""
    rows = [r for r in rows if any(r)] if rows else []
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    header_idx, preamble = detect_header_row(rows)
    cols = clean_columns(rows[header_idx])
    sheet_label = f"Sheet {sheet}" if sheet else "Table"
    docs: list[Document] = []

    if preamble.strip():
        docs.append(
            Document(
                page_content=f"Notes ({sheet_label}):\n{preamble.strip()}",
                metadata={"section": sheet_label},
            )
        )

    prefix = f"{sheet_label} columns: {' | '.join(cols)}\n"
    budget = max(max_chars - len(prefix), 200)
    buf: list[str] = []
    buf_chars = 0
    start = end = 0

    def flush() -> None:
        docs.append(
            Document(
                page_content=(prefix + "\n".join(buf)).strip(),
                metadata={"section": f"{sheet_label}, rows {start}-{end}", "pre_chunked": True},
            )
        )

    for offset, row in enumerate(rows[header_idx + 1 :], start=header_idx + 2):
        line = _render_row(row)[:budget]
        if not line.strip(" |"):
            continue
        if buf and (len(buf) >= SHEET_MAX_ROWS_PER_CHUNK or buf_chars + len(line) + 1 > budget):
            flush()
            buf, buf_chars = [], 0
        if not buf:
            start = offset
        end = offset
        buf.append(line)
        buf_chars += len(line) + 1
    if buf:
        flush()
    return docs


def _rows(values: Iterable[Iterable[Any]]) -> list[list[str]]:
    return [[_cell_text(v) for v in row] for row in values]


def load_xlsx(path: Path, max_chars: int) -> LoadedDocument:
    import openpyxl

    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as exc:
        raise LoadError(f"{path.name} is not a readable XLSX: {exc}") from exc
    try:
        docs: list[Document] = []
        for ws in wb.worksheets:
            docs.extend(
                sheet_to_documents(_rows(ws.iter_rows(values_only=True)), ws.title, max_chars)
            )
    finally:
        wb.close()
    return LoadedDocument(pages=docs, page_count=None)


def load_csv(path: Path, max_chars: int) -> LoadedDocument:
    text = path.read_bytes().decode("utf-8-sig", errors="replace")
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(text[:4096], ",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = _rows(csv.reader(text.splitlines(), dialect))
    return LoadedDocument(pages=sheet_to_documents(rows, None, max_chars), page_count=None)


# ------------------------------------------------------------------- HTML / text


def load_html(path: Path) -> LoadedDocument:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(path.read_bytes(), "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n"))
    text = "\n".join(line.strip() for line in text.splitlines())
    title = soup.title.string.strip() if soup.title and soup.title.string else None
    meta = {"section": title[:200]} if title else {}
    cleaned = _clean(text)
    return LoadedDocument(
        pages=[Document(page_content=cleaned, metadata=meta)] if cleaned else [], page_count=None
    )


def load_text(path: Path) -> LoadedDocument:
    text = _clean(path.read_bytes().decode("utf-8-sig", errors="replace"))
    return LoadedDocument(pages=[Document(text)] if text else [], page_count=None)


# ------------------------------------------------------------------------ dispatch


def load_file(
    path: Path, ext: str, *, sheet_chunk_chars: int = 1800, ocr: OcrOptions | None = None
) -> LoadedDocument:
    ext = ext.lower().lstrip(".")
    if ext == "pdf":
        return load_pdf(path, ocr)
    if ext in IMAGE_EXTENSIONS:
        return load_image(path, ocr)
    if ext == "docx":
        return load_docx(path)
    if ext == "pptx":
        return load_pptx(path)
    if ext == "xlsx":
        return load_xlsx(path, sheet_chunk_chars)
    if ext == "csv":
        return load_csv(path, sheet_chunk_chars)
    if ext in ("html", "htm"):
        return load_html(path)
    if ext in ("txt", "md"):
        return load_text(path)
    raise LoadError(f"Unsupported file type: .{ext}")


# Table-of-contents filter, ported from rag-claude. A dotted leader (3+ dots)
# followed by a page number; plain trailing numbers are common in real content.
_TOC_LINE = re.compile(r".{5,}\.{3,}\s*\d+\s*$")


def is_toc_chunk(text: str, page: int | None = None) -> bool:
    """True for table-of-contents chunks: >=2 leader lines on an early page,
    or more than 40% leader lines anywhere."""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if len(lines) < 2:
        return False
    toc = sum(1 for line in lines if _TOC_LINE.match(line))
    if page is not None and page <= 3 and toc >= 2:
        return True
    return toc / len(lines) > 0.4
