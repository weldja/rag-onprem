"""Tiny dependency-free PDF writer for tests and the sample document.

python -m tests.pdfgen ../samples/sample.pdf   # regenerate the sample
"""

import sys
from pathlib import Path

# A fictional handbook. Each inner list is one page of pre-wrapped lines.
SAMPLE_HANDBOOK: list[list[str]] = [
    [
        "Northwind Tools Ltd - Staff Handbook (fictional sample)",
        "",
        "This handbook explains the policies that apply to all employees.",
        "It covers annual leave, expenses and IT security.",
        "Questions about this handbook should go to the People team.",
    ],
    [
        "Section 2: Annual leave",
        "",
        "Full-time employees receive 27 days of annual leave per year,",
        "in addition to public bank holidays.",
        "Part-time employees receive a pro-rata entitlement.",
        "Up to 5 unused days may be carried over into the next leave year.",
        "Carried-over days must be taken by 31 March, or they are lost.",
        "Leave requests need manager approval at least two weeks in advance.",
    ],
    [
        "Section 3: Expenses",
        "",
        "Business mileage in a private car is reimbursed at 45p per mile",
        "for the first 10,000 miles in a tax year, and 25p per mile after that.",
        "Claims must be submitted within 60 days with receipts attached.",
        "Hotel stays are capped at 140 pounds per night outside London.",
    ],
    [
        "Section 4: IT security",
        "",
        "Passwords must be at least 14 characters long.",
        "Passwords are changed only when a compromise is suspected.",
        "Multi-factor authentication is required for email and VPN access.",
        "Lost or stolen laptops must be reported to the IT desk within 24 hours.",
    ],
]


def _escape(line: str) -> str:
    return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[list[str]]) -> bytes:
    objects: list[bytes] = []
    page_obj_nums = [4 + 2 * i for i in range(len(pages))]
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{n} 0 R" for n in page_obj_nums)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )
    for i, lines in enumerate(pages):
        content_num = page_obj_nums[i] + 1
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_num} 0 R >>"
            ).encode()
        )
        ops = ["BT", "/F1 11 Tf", "15 TL", "72 720 Td"]
        ops += [f"({_escape(line)}) Tj T*" for line in lines]
        ops.append("ET")
        stream = "\n".join(ops).encode("cp1252")
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for num, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "sample.pdf")
    target.write_bytes(make_pdf(SAMPLE_HANDBOOK))
    print(f"wrote {target}")
