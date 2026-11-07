"""Local OCR for scanned PDFs and image files.

Tesseract (Apache-2.0) runs as a subprocess on images rendered by pypdfium2
(Apache-2.0/BSD). Both are installed in the image at build time; neither
makes network calls. Language packs are chosen at build time (OCR_LANGUAGES
build arg) and must cover the OCR_LANGUAGES setting.
"""

import io
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.Image import Image

log = logging.getLogger(__name__)


class OcrError(RuntimeError):
    pass


@dataclass(frozen=True)
class OcrOptions:
    enabled: bool = True
    languages: str = "eng"  # tesseract codes joined with '+', e.g. "eng+deu"
    dpi: int = 300
    min_chars: int = 30  # pages with less extractable text than this get OCR
    max_pages: int = 300  # per document, to bound ingestion time
    timeout_s: float = 120.0  # per page


@cache
def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def ocr_image(image: "Image", options: OcrOptions) -> str:
    if not tesseract_available():
        raise OcrError("Tesseract is not installed in this image")
    buf = io.BytesIO()
    image.convert("L").save(buf, format="PNG")
    try:
        proc = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", options.languages, "--psm", "3"],
            input=buf.getvalue(),
            capture_output=True,
            timeout=options.timeout_s,
            # One thread per page keeps several workers from oversubscribing the CPU.
            env={**os.environ, "OMP_THREAD_LIMIT": "1"},
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise OcrError(f"OCR timed out after {options.timeout_s:.0f}s") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise OcrError(f"Tesseract failed: {detail[-1] if detail else proc.returncode}")
    return proc.stdout.decode("utf-8", "replace")


def render_pdf_page(path: Path, index: int, dpi: int) -> "Image":
    import pypdfium2

    pdf = pypdfium2.PdfDocument(str(path))
    try:
        page = pdf[index]
        try:
            image: Image = page.render(scale=dpi / 72).to_pil()
            return image
        finally:
            page.close()
    finally:
        pdf.close()


def image_frames(path: Path) -> list["Image"]:
    """Every frame of an image file (multi-page TIFFs have several)."""
    from PIL import Image as PILImage
    from PIL import ImageSequence

    with PILImage.open(path) as img:
        return [frame.copy() for frame in ImageSequence.Iterator(img)]
