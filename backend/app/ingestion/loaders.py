"""Document loaders. Milestone 1 handles text-based PDFs only."""

from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.ingestion.chunking import Document


class LoadError(ValueError):
    """The file can't be read as a supported document."""


@dataclass
class LoadedDocument:
    pages: list[Document]  # one per page with extractable text; metadata["page"] is 1-based
    page_count: int


def load_pdf(path: Path) -> LoadedDocument:
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise LoadError(f"{path.name} is encrypted")
        pages: list[Document] = []
        for number, page in enumerate(reader.pages, start=1):
            # Postgres text columns reject NUL bytes, which some PDFs produce.
            text = (page.extract_text() or "").replace("\x00", "").strip()
            if text:
                pages.append(Document(page_content=text, metadata={"page": number}))
        return LoadedDocument(pages=pages, page_count=len(reader.pages))
    except PdfReadError as exc:
        raise LoadError(f"{path.name} is not a readable PDF: {exc}") from exc
