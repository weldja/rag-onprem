"""Builds the evaluation corpus: six fictional company documents, one per
supported format, with facts the questions in cases.json ask about. Generated
in code so the corpus needs no download and every fact is known."""

import io
from pathlib import Path

from tests.pdfgen import SAMPLE_HANDBOOK, make_pdf


def handbook_pdf() -> bytes:
    pages = [list(p) for p in SAMPLE_HANDBOOK]
    pages[0][0] = "Northwind Tools Ltd - Staff Handbook 2026 (evaluation copy)"
    pages.append(
        [
            "Section 5: Working hours",
            "",
            "Core hours are 10:00 to 16:00, Monday to Friday.",
            "Outside core hours staff may work flexibly, agreed with their manager.",
            "Overtime must be approved in advance and is paid at 1.5 times the hourly rate.",
        ]
    )
    return make_pdf(pages)


def security_docx() -> bytes:
    import docx

    d = docx.Document()
    d.add_heading("IT Security Policy", level=0)
    d.add_paragraph("This policy applies to all staff, contractors and visitors using company IT.")
    sections = {
        "Passwords and MFA": [
            "Passwords must be at least 14 characters long and stored in the company "
            "password manager.",
            "Multi-factor authentication is mandatory for all remote access.",
            "Administrators must use a hardware security key.",
        ],
        "Laptops": [
            "All laptops use full-disk encryption.",
            "Laptops are replaced every 4 years.",
            "A lost or stolen laptop must be reported to the IT desk within 24 hours.",
        ],
        "Email": [
            "Messages from outside the company are tagged [EXTERNAL] in the subject line.",
            "Report suspected phishing with the Report Phish button in Outlook; do not forward it.",
        ],
        "Data classification": [
            "Information is classified as Public, Internal, Confidential or Restricted.",
            "Restricted data must never leave the UK and must not be stored on personal devices.",
        ],
    }
    for heading, paragraphs in sections.items():
        d.add_heading(heading, level=1)
        for p in paragraphs:
            d.add_paragraph(p)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def expenses_xlsx() -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Mileage"
    ws.append(["Expense rates 2026"])
    ws.append([])
    ws.append(["Vehicle", "Rate first 10,000 miles", "Rate after 10,000 miles"])
    for row in (["Car", "45p", "25p"], ["Motorcycle", "24p", "24p"], ["Bicycle", "20p", "20p"]):
        ws.append(row)
    sub = wb.create_sheet("Subsistence")
    sub.append(["Item", "Limit (pounds)", "Notes"])
    sub.append(["Breakfast", 10, "Only when travelling before 7am"])
    sub.append(["Lunch", 15, "Only on trips over 5 hours"])
    sub.append(["Dinner", 30, "Only with an overnight stay"])
    sub.append(["Incidentals", 5, "Per night away"])
    hotels = wb.create_sheet("Hotels")
    hotels.append(["City", "Nightly cap (pounds)"])
    for row in (["London", 200], ["Edinburgh", 150], ["Manchester", 140], ["Other UK", 140]):
        hotels.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def onboarding_pptx() -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    slides = [
        ("Welcome to Northwind", "We're glad you're here. This deck covers your first months."),
        (
            "Your first week",
            "Day 1: IT setup with the IT desk at 9:30.\n"
            "Day 2: health and safety induction.\nDay 3: meet your team.",
        ),
        (
            "Probation",
            "Probation lasts 6 months.\nReviews take place at 3 months and at 6 months.",
        ),
        (
            "Benefits",
            "Pension: the company contributes 5% and you contribute 3%.\n"
            "Cycle-to-work scheme available after probation.",
        ),
        ("Who to ask", "People team: people@northwind.example\nIT desk: extension 2222"),
    ]
    prs = Presentation()
    for title, body in slides:
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        slide.shapes.title.text = title
        box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(8), Inches(4))
        box.text_frame.text = body
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def facilities_html() -> bytes:
    return b"""<!doctype html><html><head><title>Office facilities guide</title></head><body>
<h1>Office facilities</h1>
<p>The office is open from 07:00 to 20:00, Monday to Friday. The building is closed at weekends.</p>
<h2>Visitors</h2><p>Visitors collect a badge from reception and must be accompanied.</p>
<h2>Parking and cycling</h2>
<p>There are 40 parking spaces, allocated by a monthly ballot run by facilities.</p>
<p>Bike storage is in the basement; showers are on the second floor.</p>
<h2>Fire safety</h2><p>The fire assembly point is car park B.</p>
</body></html>"""


def contract_md() -> bytes:
    return b"""# Supplier contract summary: Acme Cleaning Services

- Term: 3 years from 1 April 2025.
- Notice period: either party may end the contract with 90 days' written notice.
- Fee: 3,200 pounds per month, invoiced monthly in arrears.
- Service credits: if the monthly audit score falls below 85%, Acme credits 10% of that
  month's fee.
- Contract owner: the facilities manager.
"""


def complaints_docx() -> bytes:
    """A short document of many small sections (one chunk each). Sampling 6 of
    its 9 chunks for a summary skipped sections 1, 4 and 7, so the response
    deadline and record retention below must appear in a summary."""
    import docx

    d = docx.Document()
    d.add_heading("Customer Complaints Procedure", level=0)
    d.add_paragraph("Reference CCP-2 | Owner: Customer Service Manager")
    sections = {
        "Our commitment": "We want to hear when our service falls short so we can fix it.",
        "How to complain": "Write to the customer service team or use the feedback form.",
        "Acknowledgement": "Every complaint is acknowledged within 3 working days.",
        "Full response": "A full written response is sent within 20 working days.",
        "Escalation": "Customers who remain unhappy may ask a director to review the case.",
        "Billing disputes": "Disputed invoices are put on hold while a complaint is open.",
        "Record keeping": "Complaint records are kept for 4 years and reviewed every quarter.",
        "Contact": "Customer service can be reached on extension 3100.",
    }
    for heading, text in sections.items():
        d.add_heading(heading, level=1)
        d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


CORPUS = {
    "northwind-handbook-2026.pdf": handbook_pdf,
    "it-security-policy.docx": security_docx,
    "expense-rates-2026.xlsx": expenses_xlsx,
    "onboarding-2026.pptx": onboarding_pptx,
    "office-facilities.html": facilities_html,
    "acme-cleaning-contract.md": contract_md,
    "complaints-procedure.docx": complaints_docx,
}


def build(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, make in CORPUS.items():
        path = directory / name
        path.write_bytes(make())
        paths.append(path)
    return paths
