"""Builds 'scanned' test documents: text rendered into images, so the files
have no text layer and can only be read with OCR."""

import io

from PIL import Image, ImageDraw, ImageFont

from tests.pdfgen import make_pdf


def page_image(lines: list[str], size: tuple[int, int] = (1700, 2200)) -> Image.Image:
    img = Image.new("L", size, 255)
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=44)  # bundled with Pillow >= 10.1
    y = 160
    for line in lines:
        draw.text((140, y), line, fill=0, font=font)
        y += 80
    return img


def scanned_pdf(pages: list[list[str]]) -> bytes:
    """Image-only PDF, like the output of an office scanner."""
    images = [page_image(p).convert("RGB") for p in pages]
    buf = io.BytesIO()
    images[0].save(buf, format="PDF", save_all=True, append_images=images[1:], resolution=200)
    return buf.getvalue()


def mixed_pdf(text_page: list[str], scanned_page: list[str]) -> bytes:
    """Page 1 has a text layer, page 2 is a scan."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for blob in (make_pdf([text_page]), scanned_pdf([scanned_page])):
        for page in PdfReader(io.BytesIO(blob)).pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def png(lines: list[str]) -> bytes:
    buf = io.BytesIO()
    page_image(lines).save(buf, format="PNG")
    return buf.getvalue()


def tiff(pages: list[list[str]]) -> bytes:
    frames = [page_image(p) for p in pages]
    buf = io.BytesIO()
    frames[0].save(buf, format="TIFF", save_all=True, append_images=frames[1:])
    return buf.getvalue()
